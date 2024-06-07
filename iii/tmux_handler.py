import os
import subprocess
from threading import Thread, Event
from time import sleep

class TmuxHandler:
    def __init__(self):
        self._tmuxinator_project = os.environ.get('TMUXINATOR_PROJECT', None)

        if self._tmuxinator_project is not None:
            cmd = ['tmux', 'ls']
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            self._tmux_session_running = self._tmuxinator_project in stdout.decode('utf-8')
        else:
            self._tmux_session_running = False
            
    @property
    def session_running(self) -> bool:
        return self._tmux_session_running
    
    @property
    def tmuxinator_project(self) -> str:
        return self._tmuxinator_project
    
    def start(
        self,
        attach: bool = False
    ) -> bool:
        if self._tmuxinator_project is None:
            print('TMUXINATOR_PROJECT environment variable not set. Cannot boot system.')
            return False

        if self._tmux_session_running:
            print ('System already booted. Use "iii system attach" to attach to the tmux session.')
            
            return False
        
        cmd = ['tmuxinator', 'start', self._tmuxinator_project, ('--attach' if attach else '--no-attach')]
        
        # Start tmux session, exit this program, but attach to tmux session if requested
        process = subprocess.run(cmd)

        if not attach:
            if process.returncode != 0:
                print('Failed to boot system.')
                
                return False
            
            print('System booted. Use "iii system start" to start the system.')
            
            return True
            
        return True

    def attach(self):
        if self._tmuxinator_project is None:
            print('TMUXINATOR_PROJECT environment variable not set. Cannot attach to tmux session.')
            return False
        
        if self._tmux_session_running:
            cmd = ['tmux', 'attach', '-t', self._tmuxinator_project]
            
            subprocess.run(cmd)
            
            return True
            
        else:
            print('Session not running. Use "iii system boot --attach" to start the system and attach to the tmux session.')
            
            return False
        
    def kill_session(self):
        if self._tmuxinator_project is None:
            return False
        
        process = subprocess.Popen(['tmuxinator', 'stop', self._tmuxinator_project], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.wait()
        
        if process.returncode != 0:
            print('Failed to kill tmux session.')
            return False
        
        print('Tmux session killed.')
        
        return True