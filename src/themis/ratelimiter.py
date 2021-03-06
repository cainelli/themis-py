"""
This module implements a RateLimiter class.
RateLimiter is a Redis backed object used to define one or more rules to rate limit requests.

This module can be run to show an example of a running RateLimiter instance.
"""
import logging, math, redis, time
from itertools import izip

class RateLimiter(object):
  """
  RateLimiter is used to define one or more rate limit rules.
  These rules are checked on .acquire() and we either return True or False based on if we can make the request,
  or we can block until we make the request.
  Manual blocks are also supported with the block method.
  """

  def __init__(self, redis, redis_namespace, conditions=None):
    """
    Initalize an instance of a RateLimiter

    conditions - list or tuple of rate limit rules
    redis_host - Redis host to use
    redis_port - Redis port (if different than default 6379)
    redis_db   - Redis DB to use (if different than 0)
    redis_password - Redis password (if needed)
    redis_namespace - Redis key namespace
    """

    #self.redis = redis.Redis(host=redis_host, port=redis_port, db=redis_db, password=redis_password)
    self.redis = redis
    self.log = logging.getLogger(__name__)
    self.namespace = redis_namespace
    self.conditions = []
    self.list_ttl = 0

    if conditions:
      self.add_condition(*conditions)

  def add_condition(self, *conditions):
    """
    Adds one or more conditions to this RateLimiter instance
    Conditions can be given as:
      add_condition(1, 10)
      add_condition((1, 10))
      add_condition((1, 10), (30, 600))
      add_condition({'requests': 1, 'seconds': 10})
      add_condition({'requests': 1, 'seconds': 10}, {'requests': 200, 'hours': 6})

      dict can contain 'seconds', 'minutes', 'hours', and 'days' time period parameters
    """
    # allow add_condition(1,2) as well as add_condition((1,2))
    if len(conditions) == 2 and isinstance(conditions[0], int):
      conditions = [conditions]

    for condition in conditions:
      if isinstance(condition, dict):
        requests = condition['requests']
        seconds = condition.get('seconds', 0) + (
                    60 * (condition.get('minutes', 0) + 
                    60 * (condition.get('hours', 0) + 
                    24 * condition.get('days', 0))))
      else:
        requests, seconds = condition

      # requests and seconds always a positive integer
      requests = int(requests)
      seconds = int(seconds)

      if requests < 0:
        raise ValueError('negative number of requests (%s)' % requests)
      if seconds < 0:
        raise ValueError('negative time period given (%s)' % seconds)

      if seconds > 0:
        if requests == 0:
          self.log.warn('added block all condition (%s/%s)', requests, seconds)
        else:
          self.log.debug('added condition (%s/%s)', requests, seconds)

        self.conditions.append((requests, seconds))

        if seconds > self.list_ttl:
          self.list_ttl = seconds
      else:
        self.log.warn('time period of 0 seconds. not adding condition')

    # sort by requests so we query redis list in order as well as know max and min requests by position
    self.conditions.sort()

  def block(self, key, seconds=0, minutes=0, hours=0, days=0):
    """
    Set manual block for key for a period of time
    key - key to track what to rate limit
    Time parameters are added together and is the period to block for
      seconds
      minutes
      hours
      days
    """
    seconds = seconds + 60 * (minutes + 60 * (hours + 24 * days))
    # default to largest time period we are limiting by
    if not seconds:
      seconds = self.list_ttl

      if not seconds:
        self.log.warn('block called but no default block time. not blocking')
        return 0

    if not isinstance(seconds, int):
      seconds = int(math.ceil(seconds))

    key = ':'.join(('block', self.namespace, key))
    self.log.warn('block key (%s) for %ds', key, seconds)
    with self.redis.pipeline() as pipe:
      pipe.set(key, '1')
      pipe.expire(key, seconds)
      pipe.execute()

    return seconds

  def is_manual_block(self, key):
    block_key = ':'.join(('block', self.namespace, key))
    log_key = ':'.join(('rate', self.namespace, key))
    block_ttl = int(self.redis.ttl(block_key))
    if block_ttl >= 0:
      self.redis.delete(log_key)
    return block_ttl

  def acquire(self, key, block_size=1, block=True):
    """
    Tests whether we can make a request, or if we are currently being limited
    key - key to track what to rate limit
    block - Whether to wait until we can make the request
    """
    if block:
      while True:
        success, wait = self._make_ping(key)
        if success:
          return True, wait
        self.log.debug('blocking acquire sleeping for %.1fs', wait)
        time.sleep(wait)
    else:
      for _ in range(0, block_size):
        success, wait = self._make_ping(key)
        if not success:
          return success, wait
      return success, wait

  # alternative acquire interface ratelimiter(key)
  __call__ = acquire

  def _make_ping(self, key):

    # shortcut if no configured conditions 
    if not self.conditions:
      return True, 0.0

    # short cut if we are limiting to 0 requests
    min_requests, min_request_seconds = self.conditions[0]
    if min_requests == 0:
      self.log.warn('(%s) hit block all limit (%s/%s)', key, min_requests, min_request_seconds)
      return False, min_request_seconds

    log_key = ':'.join(('rate', self.namespace, key))
    block_key = ':'.join(('block', self.namespace, key))
    lock_key = ':'.join(('lock', self.namespace, key))

    with self.redis.lock(lock_key, timeout=10):

      with self.redis.pipeline() as pipe:
        for requests, _ in self.conditions:
          pipe.lindex(log_key, requests-1) # subtract 1 as 0 indexed

        # check manual block keys
        pipe.ttl(block_key)
        pipe.get(block_key)
        boundry_timestamps = pipe.execute()

      blocked = boundry_timestamps.pop()
      block_ttl = boundry_timestamps.pop()

      if blocked is not None:
        # block_ttl is None for last second of a keys life. set min of 0.5
        if block_ttl is None:
          block_ttl = 0.5
        self.log.warn('(%s) hit manual block. %ss remaining', key, block_ttl)
        return False, block_ttl

      timestamp = time.time()

      for boundry_timestamp, (requests, seconds) in izip(boundry_timestamps, self.conditions):
        # if we dont yet have n number of requests boundry_timestamp will be None and this condition wont be limiting
        if boundry_timestamp is not None:
          boundry_timestamp = float(boundry_timestamp)
          if boundry_timestamp + seconds > timestamp:
            # Here we need extract statistics
            self.log.warn('(%s) hit limit (%s/%s) time to allow %.1fs',
                  key, requests, seconds, boundry_timestamp + seconds - timestamp)
            return False, boundry_timestamp + seconds - timestamp

      # record our success
      with self.redis.pipeline() as pipe:
        pipe.lpush(log_key, timestamp)
        max_requests, _ = self.conditions[-1]
        pipe.ltrim(log_key, 0, max_requests-1) # 0 indexed so subtract 1
        # if we never use this key again, let it fall out of the DB after max seconds has past
        pipe.expire(log_key, self.list_ttl)
        pipe.execute()

    return True, 0.0


if __name__ == '__main__':
  """
  This is an example of rate limiting using the RateLimiter class
  """
  import sys
  logging.basicConfig(format='%(asctime)s %(process)s %(levelname)s %(name)s %(message)s', level=logging.DEBUG, stream=sys.stdout)
  log = logging.getLogger('ratelimit.main')
  key = 'TestRateLimiter'
  redis = redis.StrictRedis('localhost', db=4)

  rate = RateLimiter(redis, 'bla')
  #rate.add_condition((3, 10), (4, 15))
  #rate.add_condition({'requests':20, 'minutes':5})
  rate.add_condition({'requests':2, 'seconds':3})
  rate.add_condition({'requests':3, 'minutes':1})
  #rate.custom_block = True
  #rate.list_ttl = 10

  i = 1
  #for _ in xrange(100):
  #rate.custom_block = 20
  #success, wait = rate.acquire(key, 1, False)
  #print rate.block(key, seconds=20)
  #rate.block(key, seconds=20)
  #success, wait = rate.acquire(key, 1, False)
  #print success, wait, rate.conditions
  #log.info('***************     ping %d     ***************', i)

  success, wait = rate.acquire(key, 1, False)
  print success, wait
  if success is False:
    if not rate.is_manual_block(key): 
      rate.block(key, seconds=20)
  #for _ in xrange(10):
  #    rate.acquire(key)
  #    log.info('***************     ping %d     ***************', i)
  #    i+=1

  # block all keys
  #rate.add_condition(0, 1)

  #for _ in xrange(5):
  #    rate(key, block=False) # alternative interface
  #    time.sleep(1)
