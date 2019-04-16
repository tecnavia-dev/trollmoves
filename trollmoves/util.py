#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2012, 2013, 2014, 2015, 2016

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>
#   Panu Lahtinen <panu.lahtinen@fmi.fi>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import bz2
import errno
import fnmatch
import glob
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import time
import traceback
from ConfigParser import ConfigParser
from ftplib import FTP, all_errors
from Queue import Empty, Queue
from threading import Thread
from urlparse import urlparse, urlunparse

import pyinotify
from zmq import NOBLOCK, POLLIN, PULL, PUSH, ROUTER, Poller, ZMQError

from posttroll import context
from posttroll.message import Message
from posttroll.publisher import get_own_ip
from trollsift import globify, parse

LOGGER = logging.getLogger(__name__)

def check_output(*popenargs, **kwargs):
    """Copy from python 2.7, `subprocess.check_output`."""
    if 'stdout' in kwargs:
        raise ValueError('stdout argument not allowed, it will be overridden.')
    LOGGER.debug("Calling " + str(popenargs))
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output, unused_err = process.communicate()
    del unused_err
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise RuntimeError(output)
    return output


def check_output(*popenargs, **kwargs):
    """Copy from python 2.7, `subprocess.check_output`."""
    if 'stdout' in kwargs:
        raise ValueError('stdout argument not allowed, it will be overridden.')
    LOGGER.debug("Calling " + str(popenargs))
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output, unused_err = process.communicate()
    del unused_err
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise RuntimeError(output)
    return output

def xrit(pathname, destination=None, cmd="./xRITDecompress"):
    """Unpacks xrit data."""
    opath, ofile = os.path.split(pathname)
    destination = destination or "/tmp/"
    dest_url = urlparse(destination)
    expected = os.path.join((destination or opath), ofile[:-2] + "__")
    if dest_url.scheme in ("", "file"):
        if ofile != os.path.basename(expected):
           check_output([cmd, pathname], cwd=(destination or opath))
    else:
        LOGGER.exception("Can not extract file " + pathname + " to " +
                         destination + ", destination has to be local.")
    LOGGER.info("Successfully extracted " + pathname + " to " + destination)
    return expected

# bzip

BLOCK_SIZE = 1024


def bzip(origin, destination=None):
    """Unzip files."""
    ofile = os.path.split(origin)[1]
    destfile = os.path.join(destination or "/tmp/", ofile[:-4])
    if os.path.exists(destfile):
        return destfile
    with open(destfile, "wb") as dest:
        try:
            orig = bz2.BZ2File(origin, "r")
            while True:
                block = orig.read(BLOCK_SIZE)

                if not block:
                    break
                dest.write(block)
            LOGGER.debug("Bunzipped " + origin + " to " + destfile)
        finally:
            orig.close()
    return destfile

def unpack(pathname,
           compression=None,
           working_directory=None,
           prog=None,
           delete="False",
           **kwargs):
    """Unpack *pathname*."""
    del kwargs
    if compression:
        try:
            unpack_fun = eval(compression)
            if prog is not None:
                new_path = unpack_fun(pathname, working_directory, prog)
            else:
                new_path = unpack_fun(pathname, working_directory)
        except:
            LOGGER.exception("Could not decompress " + pathname)
        else:
            if delete.lower() in ["1", "yes", "true", "on"]:
                os.remove(pathname)
            return new_path
    return pathname

def util_purgeDir(dirBase, dirToLeaveSize):
    # Purge directory to max size
    toret = 0
    dest_list = os.listdir(dirBase)
    dest_listsubdir = []
    for dest_dir in dest_list:
        if os.path.isdir(os.path.join(dirBase, dest_dir)):
            dest_listsubdir.append(dest_dir)
    print "List subdir (" + dirBase + "):"
    print dest_listsubdir
    print "Size: " + str(len(dest_listsubdir)) + " To Leave: " + str(dirToLeaveSize) 
    if len(dest_listsubdir) > dirToLeaveSize:
        dest_listsubdir.sort()
        print "Sorted destination: "
        print dest_listsubdir
        dest_todel = len(dest_listsubdir) - dirToLeaveSize
        for x in range(0, dest_todel):
            dest_dirtodel = os.path.join(dirBase, dest_listsubdir[x])
            print "Todel " + dest_dirtodel
            shutil.rmtree(dest_dirtodel)
            toret += 1

    return toret

def util_generateRef(destDir, destFile, destDirRef):
    dest_epistr = "[REF]\r\n"
    dest_epistr += "SourcePath = " + destDir + "\r\n"
    dest_epistr += "FileName = " + destFile + "\r\n"
    dest_epifile = destDirRef + "/" + destFile
    dest_epifilefp = open(dest_epifile, "w")
    dest_epifilefp.write(dest_epistr)
    dest_epifilefp.close()

    return dest_epifile

def util_touchRef(destDir, destFile, destDirRef):
   dest_epifile = None
   for fname in os.listdir(destDir):
       if fname.find("-EPI")>0:
           dest_epifile = destDirRef + "/" + fname
           destFile = fname
   if dest_epifile is not None:
      if os.path.isfile(dest_epifile):
         #touch the ref file
         os.remove(dest_epifile)
         util_generateRef(destDir, destFile, destDirRef)

# Mover
