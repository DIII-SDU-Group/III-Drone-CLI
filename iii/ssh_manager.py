import subprocess
import os
import getpass

class SSHManager:
    def __init__(self):
        for i in range(3):
            self._host = self._get_host()
            self._user = self._get_user()
            self._password = self._get_password()

            can_connect = self._can_connect()
        
            if not can_connect and i < 2:
                print('Could not connect to host. Please try again.')
                
                self._clear_password()
                
            elif not can_connect:
                print('Could not connect to host. Exiting...')
                self._clear_password()
                exit(1)

    def execute(
        self, 
        command
    ):
        # Return (command succeeded, connection succeeded)
        process = subprocess.Popen(
            f"sshpass -p $(cat /tmp/III_SSH_PASSWORD) ssh -o StrictHostKeyChecking=no {self._user}@{self._host} \"{command}\"",
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            if process.returncode == 255:
                return False, False
            else:
                return False, True
        
        return True, True

    def transfer_to_host(
        self,
        source,
        destination,
        recursive=False,
        force=False
    ):
        # Return (transfer succeeded, connection succeeded)
        if recursive:
            recursive_flag = '-r'
        else:
            recursive_flag = ''
        
        if force:
            force_flag = '-f'
        else:
            force_flag = ''
            
        process = subprocess.Popen(
            f"sshpass -p $(cat /tmp/III_SSH_PASSWORD) scp {recursive_flag} {force_flag} {source} {self._user}@{self._host}:{destination}",
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            if process.returncode == 255:
                return False, False
            else:
                return False, True
            
        return True, True
    
    def sync(
        self,
        source,
        destination,
        exclude_dirs=[],
    ):
        # Return (sync succeeded, connection succeeded)
        if source[-1] != '/':
            source += '/'
            
        if destination[-1] == '/':
            destination = destination[:-1]
            
        process = subprocess.Popen(
            f"sshpass -p $(cat /tmp/III_SSH_PASSWORD) rsync --rsync-path=\"mkdir -p {destination} && rsync\" -av --delete {' '.join([f'--exclude={dir}' for dir in exclude_dirs])} {source} {self._user}@{self._host}:{destination}",
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            if process.returncode == 255:
                return False, False
            else:
                return False, True
            
        return True, True

    def open_session(self):
        os.system(f"sshpass -p $(cat /tmp/III_SSH_PASSWORD) ssh -o StrictHostKeyChecking=no {self._user}@{self._host}")
                
    def _clear_password(self):
        os.remove('/tmp/III_SSH_PASSWORD')
        
    def _get_host(self):
        host = os.getenv('III_SSH_HOST')
        
        if host is None:
            # Prompt user for host
            host = input('Enter the host: ')
            os.environ['III_SSH_HOST'] = host
            
        return host
    
    def _get_user(self):
        user = os.getenv('III_SSH_USER')
        
        if user is None:
            # Prompt user for user
            user = input('Enter the user: ')
            os.environ['III_SSH_USER'] = user
            
        return user
    
    def _get_password(self):
        # If /tmp/III_SSH_PASSWORD exists, use that
        if os.path.exists('/tmp/III_SSH_PASSWORD'):
            with open('/tmp/III_SSH_PASSWORD', 'r') as f:
                password = f.read().strip()
                
            return password
        
        # Prompt user for password
        password = getpass.getpass('Enter the password: ')

        with open('/tmp/III_SSH_PASSWORD', 'w') as f:
            f.write(password)
            
        return password
    
    def _can_connect(self):
        try:
            command = [
                'sshpass',
                '-p',
                '$(cat /tmp/III_SSH_PASSWORD)',
                'ssh',
                '-o',
                'StrictHostKeyChecking=no',
                f'{self._user}@{self._host}',
                'exit',
            ]
            
            subprocess.run(
                " ".join(command),
                shell=True,
                check=True,
            )
            return True
        except subprocess.CalledProcessError:
            return False