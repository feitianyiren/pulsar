'''\
Testing application. Pulsar tests uses exatly the same API as any
pulsar server. The Test suite is the Arbiter while the
Worker class runs the tests in an asychronous way.
'''
import unittest
import logging
import os
import time
import inspect

import pulsar
from pulsar.utils.importer import import_module
from pulsar.utils.async import make_deferred,\
                               Deferred, simple_callback, async


logger = logging.getLogger()

LOGGING_MAP = {1: logging.CRITICAL,
               2: logging.INFO,
               3: logging.DEBUG}


class Silence(logging.Handler):
    def emit(self, record):
        pass


class TestCase(unittest.TestCase):
    '''A specialised test case which offers three
additional functions:

a) 'initTest' and 'endTests', called at the beginning and at the end
of the tests declared in a derived class. Useful for starting a server
to send requests to during tests.

b) 'runInProcess' to run a callable in the main process.'''
    suiterunner = None
    
    def __init__(self, methodName=None):
        if methodName:
            self._dummy = False
            super(TestCase,self).__init__(methodName)
        else:
            self._dummy = True
    
    def __repr__(self):
        if self._dummy:
            return self.__class__.__name__
        else:
            return super(TestCase,self).__repr__()
        
    def sleep(self, timeout):
        time.sleep(timeout)        

    def initTests(self):
        pass
    
    def endTests(self):
        pass
    

class TestSuite(unittest.TestSuite):
    '''A test suite for the modified TestCase.'''
    loader = unittest.TestLoader()
    
    def addTest(self, test):
        tests = self.loader.loadTestsFromTestCase(test)
        if tests:
            try:
                obj = test()
            except:
                obj = test
            self._tests.append({'obj':obj,
                                'tests':tests})
    
    def _runtests(self, res, tests, end, result):
        if isinstance(res,Deferred):
            return res.add_callback(lambda x : self._runtests(x,tests,end,result))
        else:
            for t in tests:
                t(result)
            if end:
                end()
            return result
        
    @async
    def run(self, result):
        for test in self:
            if result.shouldStop:
                raise StopIteration
            obj = test['obj']
            init = getattr(obj,'initTests',None)
            end = getattr(obj,'endTests',None)
            if init:
                yield init()
            for t in test['tests']:
                yield t(result)
            if end:
                yield end()
        yield result
        
class TestLoader(object):
    '''Load test cases'''
    suiteClass = TestSuite
    
    def __init__(self, tags, testtype, extractors, itags):
        self.tags = tags
        self.testtype = testtype
        self.extractors = extractors
        self.itags = itags
        
    def load(self, suiterunner):
        """Return a suite of all tests cases contained in the given module.
It injects the suiterunner proxy for comunication with the master process."""
        itags = self.itags or []
        tests = []
        for module in self.modules():
            for name in dir(module):
                obj = getattr(module, name)
                if inspect.isclass(obj) and issubclass(obj, unittest.TestCase):
                    tag = getattr(obj,'tag',None)
                    if tag and not tag in itags:
                        continue
                    obj.suiterunner = suiterunner
                    obj.log = logging.getLogger(obj.__class__.__name__)
                    tests.append(obj)
        return self.suiteClass(tests)
    
    def get_tests(self,dirpath):
        join  = os.path.join
        loc = os.path.split(dirpath)[1]
        for d in os.listdir(dirpath):
            if d.startswith('__'):
                continue
            if os.path.isdir(join(dirpath,d)):
                yield (loc,d)
            
    def modules(self):
        tags,testtype,extractors = self.tags,self.testtype,self.extractors
        for extractor in extractors:
            testdir = extractor.testdir(testtype)
            for loc,app in self.get_tests(testdir):
                if tags and app not in tags:
                    logger.debug("Skipping tests for %s" % app)
                    continue
                logger.debug("Try to import tests for %s" % app)
                test_module = extractor.test_module(testtype,loc,app)
                try:
                    mod = import_module(test_module)
                except ImportError as e:
                    logger.debug("Could not import tests for %s: %s" % (test_module,e))
                    continue
                
                logger.debug("Adding tests for %s" % app)
                yield mod
    
class TextTestRunner(unittest.TextTestRunner):
    
    def run(self, test):
        "Run the given test case or test suite."
        result = self._makeResult()
        result.startTime = time.time()
        startTestRun = getattr(result, 'startTestRun', None)
        if startTestRun is not None:
            startTestRun()
        return test(result).add_callback(self.end)
            
    def end(self, result):
        stopTestRun = getattr(result, 'stopTestRun', None)
        if stopTestRun is not None:
            stopTestRun()
        result.stopTime = time.time()
        timeTaken = result.stopTime - result.startTime
        result.printErrors()
        if hasattr(result, 'separator2'):
            self.stream.writeln(result.separator2)
        run = result.testsRun
        self.stream.writeln("Ran %d test%s in %.3fs" %
                            (run, run != 1 and "s" or "", timeTaken))
        self.stream.writeln()

        expectedFails = unexpectedSuccesses = skipped = 0
        try:
            results = map(len, (result.expectedFailures,
                                result.unexpectedSuccesses,
                                result.skipped))
        except AttributeError:
            pass
        else:
            expectedFails, unexpectedSuccesses, skipped = results

        infos = []
        if not result.wasSuccessful():
            self.stream.write("FAILED")
            failed, errored = len(result.failures), len(result.errors)
            if failed:
                infos.append("failures=%d" % failed)
            if errored:
                infos.append("errors=%d" % errored)
        else:
            self.stream.write("OK")
        if skipped:
            infos.append("skipped=%d" % skipped)
        if expectedFails:
            infos.append("expected failures=%d" % expectedFails)
        if unexpectedSuccesses:
            infos.append("unexpected successes=%d" % unexpectedSuccesses)
        if infos:
            self.stream.writeln(" (%s)" % (", ".join(infos),))
        else:
            self.stream.write("\n")
        return result
        

def run_tests(self, suite):
    '''The tests can start only when we receive the proxy for the testsuite'''
    cfg = self.cfg
    self.loader = TestLoader(cfg.tags, cfg.testtype, cfg.extractors, cfg.itags)    
    TextTestRunner(verbosity = cfg.verbosity)\
                    .run(suite)\
                    .add_callback(simple_callback(self._shut_down))


class TestApplication(pulsar.Application):
    ArbiterClass = pulsar.Arbiter
    
    def on_arbiter_proxy(self, worker):
        cfg = worker.cfg
        suite = TestLoader(cfg.tags, cfg.testtype, cfg.extractors, cfg.itags)\
                        .load(worker.arbiter_proxy)  
        return TextTestRunner(verbosity = cfg.verbosity).run(suite)\
                        .add_callback(simple_callback(worker._shut_down))
                    
    def load_config(self, **params):
        pass
    
    def handler(self):
        return self
    
    def configure_logging(self):
        '''Setup logging'''
        verbosity = self.cfg.verbosity
        level = LOGGING_MAP.get(verbosity,None)
        if level is None:
            logger.addHandler(Silence())
        else:
            logger.addHandler(logging.StreamHandler())
            logger.setLevel(level)
        

class TestConfig(pulsar.DummyConfig):
    '''Configuration for testing'''
    def __init__(self, tags, testtype, extractors, verbosity, itags, inthread):
        self.tags = tags
        self.testtype = testtype
        self.extractors = extractors
        self.verbosity = verbosity
        self.itags = itags
        self.workers = 1
        if inthread:
            self.worker_class = pulsar.WorkerThread
        else:
            self.worker_class = pulsar.WorkerProcess
        
        
def TestSuiteRunner(tags, testtype, extractors,
                    verbosity = 1, itags = None,
                    inthread = False):
    cfg = TestConfig(tags, testtype, extractors, verbosity, itags, inthread)
    TestApplication(cfg = cfg).start()

