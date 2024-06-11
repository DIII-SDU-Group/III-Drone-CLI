import os
import subprocess

class ContainerManager:
    def __init__(self):
        self.compose_file = os.getenv('DOCKER_COMPOSE_FILE')
        self.workspace_dir = os.getenv('WORKSPACE_DIR')

    def build(self) -> bool:
        process = subprocess.Popen(
            f'docker compose -f {self.compose_file} --profile "*" build && docker compose -f {self.compose_file} --profile "*" up --no-start',
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            print('Failed to build containers')
            return False
            
        print('Successfully built containers.')
        
        return True
        
    def up(self):
        process = subprocess.Popen(
            f'docker compose -f {self.compose_file} --profile test up -d',
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            print('Failed to start container')
            return False
        
        print('Successfully started container')
        
        return True
        
    def down(self):
        process = subprocess.Popen(
            f'docker compose -f {self.compose_file} --profile test down',
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            print('Failed to stop container')
            return False
        
        print('Successfully stopped container')
        
        return True
        
    def colcon_build(
        self,
        colcon_build_args: list = []
    ) -> bool:
        if colcon_build_args is None:
            colcon_build_args = []
        # self.compose_project.start(service_names=['iii_drone_build'])

        process = subprocess.Popen(
            f'docker compose -f {self.compose_file} --profile build run --rm iii_drone_build colcon build {" ".join(colcon_build_args)}',
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            print('Failed to build system')
            return False
        
        print('Successfully built system')
        
    def execute_cli(
        self,
        command: str,
        args: list = []
    ) -> bool:
        command = f'docker compose -f {self.compose_file} --profile cli run --rm cli {command} {" ".join(args)}'
        process = subprocess.Popen(
            command,
            shell=True,
            executable='/bin/bash',
        )
        
        process.wait()
        
        if process.returncode != 0:
            return False
        
        return True
        