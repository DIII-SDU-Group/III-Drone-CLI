from setuptools import setup, find_packages

setup(
    name='iii',
    version='0.1',
    packages=find_packages(),
    install_requires=[
        'argcomplete',
    ],
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'iii = iii.__main__:main',
        ],
    },
)
