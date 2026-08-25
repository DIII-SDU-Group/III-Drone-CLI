from setuptools import setup, find_packages

setup(
    name='iii',
    version='0.2.0',
    packages=find_packages(),
    package_data={'iii': ['schemas/*.json']},
    install_requires=[
        'argcomplete>=3,<4',
    ],
    extras_require={'test': ['pytest>=7', 'jsonschema>=3.2']},
    entry_points={
        'console_scripts': [
            'iii = iii.__main__:main',
        ],
    },
)
