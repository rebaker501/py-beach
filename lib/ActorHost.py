import sys
import os
import signal
import gevent
from gevent import Greenlet
from gevent.event import Event
from Utils import *
import imp
import zmq.green as zmq
from Actor import Actor
import yaml
import time
import syslog
import traceback

timeToStopEvent = Event()

def _stopAllActors():
    global timeToStopEvent
    timeToStopEvent.set()

class ActorHost ( object ):
    
    # The actorList is a list( actorNames, configFile )
    def __init__( self, configFile, instanceId ):
        
        # Setting the signal handler to trigger the stop event which
        # is interpreted by each actor implementation
        global timeToStopEvent
        gevent.signal( signal.SIGQUIT, _stopAllActors )
        gevent.signal( signal.SIGINT, _stopAllActors )

        self._initLogging()

        self.log( "Initializing" )
        
        self.stopEvent = timeToStopEvent

        self.actors = {}

        self.py_beach_dir = None

        self.configFilePath = configFile
        self.configFile = None

        with open( self.configFilePath, 'r' ) as f:
            self.configFile = yaml.load( f )

        self.py_beach_dir = os.path.dirname( os.path.abspath( __file__ ) )

        os.chdir( os.path.dirname( os.path.abspath( self.configFilePath ) ) )

        self.codeDirectory = os.path.abspath( self.configFile.get( 'code_directory', './' ) )

        self.opsSocket = ZSocket( zmq.REP, 'ipc:///tmp/py_beach_instance_%d' % instanceId, isBind = True )
        self.log( "Listening for ops on %s" % ( 'ipc:///tmp/py_beach_instance_%d' % instanceId, ) )
        
        self.hostOpsPort = self.configFile.get( 'ops_port', 4999 )
        self.hostOpsSocket = ZSocket( zmq.REP, 'tcp://127.0.0.1:%d' % self.hostOpsPort, isBind = False )

        Actor._ActorHandle._zHostDir = self.configFile.get( 'directory_port', 'ipc:///tmp/py_beach_directory_port' )
        
        gevent.spawn( self.svc_receiveTasks )
        gevent.spawn( self.svc_monitorActors )

        self.log( "Now open to actors" )

        timeToStopEvent.wait()
        
        self.log( "Exiting, stopping all actors." )
        
        for actor in self.actors.values():
            actor.stop()
        
        gevent.joinall( self.actors.values() )
        self.log( "All Actors exiting, exiting." )
    
    def svc_receiveTasks( self ):
        while not self.stopEvent.wait( 0 ):
            self.log( "Waiting for op" )
            data = self.opsSocket.recv()
            if data is not False and 'req' in data:
                action = data[ 'req' ]
                self.log( "Received new ops request: %s" % action )
                if 'keepalive' == action:
                    self.opsSocket.send( successMessage() )
                elif 'start_actor' == action:
                    if 'actor_name' not in data or 'port' not in data or 'uid' not in data:
                        self.opsSocket.send( errorMessage( 'missing information to start actor' ) )
                    else:
                        actorName = data[ 'actor_name' ]
                        realm = data.get( 'realm', 'global' )
                        port = data[ 'port' ]
                        uid = data[ 'uid' ]
                        self.log( "Starting actor %s/%s at %s/%s.py" % ( realm,
                                                                         actorName,
                                                                         self.codeDirectory,
                                                                         actorName ) )
                        try:
                            actor = getattr( imp.load_source( '%s_%s' % ( realm, actorName ),
                                                              '%s/%s/%s.py' % ( self.codeDirectory,
                                                                                realm,
                                                                                actorName ) ),
                                             actorName )( self, realm, port, uid )
                        except:
                            actor = None

                        if actor is not None:
                            self.log( "Successfully loaded actor %s/%s" % ( realm, actorName ) )
                            self.actors[ uid ] = actor
                            actor.start()
                            self.opsSocket.send( successMessage() )
                        else:
                            self.opsSocket.send( errorMessage( 'exception',
                                                               data = { 'st' : traceback.format_exc() } ) )
                elif 'kill_actor' == action:
                    if 'uid' not in data:
                        self.opsSocket.send( errorMessage( 'missing information to stop actor' ) )
                    else:
                        uid = data[ 'uid' ]
                        if uid in self.actors:
                            actor = self.actors[ uid ]
                            del( self.actors[ uid ] )
                            actor.stop()
                            actor.join( timeout = 10 )
                            info = None
                            if not actor.ready():
                                actor.kill( timeout = 10 )
                                info = { 'error' : 'timeout' }
                            self.opsSocket.send( successMessage( data = info ) )
                        else:
                            self.opsSocket.send( errorMessage( 'actor not found' ) )
                else:
                    self.opsSocket.send( errorMessage( 'unknown request', data = { 'req' : action } ) )
            else:
                self.logCritical( "Received completely invalid request" )
                self.opsSocket.send( errorMessage( 'invalid request' ) )

    def svc_monitorActors( self ):
        while not self.stopEvent.wait( 0 ):
            self.log( "Culling actors that stopped of themselves" )
            for uid, actor in self.actors.iteritems():
                if not actor.isRunning():
                    del( self.actors[ uid ] )
                    self.hostOpsSocket.request( { 'req' : 'remove_actor', 'uid' : uid }, timeout = 5 )
            gevent.sleep( 30 )

    def _initLogging( self ):
        syslog.openlog( '%s-%d' % ( self.__class__.__name__, os.getpid() ), facility = syslog.LOG_USER )

    def log( self, msg ):
        syslog.syslog( syslog.LOG_INFO, msg )
        msg = '%s - %s : %s' % ( int( time.time() ), self.__class__.__name__, msg )
        print( msg )

    def logCritical( self, msg ):
        syslog.syslog( syslog.LOG_ERR, msg )
        msg = '!!! %s - %s : %s' % ( int( time.time() ), self.__class__.__name__, msg )
        print( msg )

if __name__ == '__main__':
    host = ActorHost( sys.argv[ 1 ], int( sys.argv[ 2 ] ) )