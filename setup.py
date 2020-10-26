# -*- coding:utf-8 -*-

from __future__ import absolute_import

from setuptools import find_packages
from setuptools import setup

version = '0.1.1'

requirements = [
    'pandas>=0.25.3',
    'scikit-learn>=0.22.1',
    'numpy>=1.17.4',
    'tables>=3.6.1',
    'lightgbm',
    'dask',
    'dask-ml',
    'dask-lightgbm',
    'dask-xgboost',
]

MIN_PYTHON_VERSION = '>=3.6.*'

long_description = open('README.md', encoding='utf-8').read()

setup(
    name='tabular-toolbox',
    version=version,
    description="A library of extension and helper modules for tabular data base on python's machine learning frameworks.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url='',
    author='DataCanvas Community',
    author_email='yangjian@zetyun.com',
    license='Apache License 2.0',
    install_requires=requirements,
    python_requires=MIN_PYTHON_VERSION,
    extras_require={
        'tests': ['pytest', ]
    },

    classifiers=[
        'Operating System :: OS Independent',
        'Intended Audience :: Developers',
        'Intended Audience :: Education',
        'Intended Audience :: Science/Research',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Topic :: Scientific/Engineering',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
        'Topic :: Software Development',
        'Topic :: Software Development :: Libraries',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    packages=find_packages(exclude=('docs', 'tests')),
    package_data={
    },
    zip_safe=False,
    include_package_data=True,
)
