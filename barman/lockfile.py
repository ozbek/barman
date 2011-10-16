#
# barman - Backup and Recovery Manager for PostgreSQL
#
# Copyright (C) 2011  Devise.IT S.r.l. <info@2ndquadrant.it>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import errno
import fcntl
import os

class LockfileBusyException(Exception):
    pass

class lockfile(object):
    """
    Ensures that there is only one process which is running against a specified lockfile.
    
    It supports the Context Manager interface, allowing the use in with statements.
    
        whith lockfile('file.lock') as locked:
            if not locked:
                print "failed"
            else:
                <do someting>
    
    You can also use exceptions on failures
    
        try:
            whith lockfile('file.lock', True):
                <do someting>
        except LockfileBusyException, e, file:
            print "failed to lock %s" % file
    
    """
    def __init__(self, filename, raise_if_fail=False, wait=False):
        self.filename = os.path.abspath(filename)
        self.fd = None
        self.raise_if_fail = raise_if_fail
        self.wait = wait

    def acquire(self, raise_if_fail=None, wait=None):
        '''
        Creates and holds on to the lock file.
        Returns True if lock successful, False if it is not.

        If raise_if_fail parameter is true, a LockfileBusyException is raised if the
        lock is held by someone else.
        '''
        if self.fd: return True
        try:
            fd = os.open(self.filename, os.O_TRUNC | os.O_CREAT | os.O_RDWR, 0600)
            flags = fcntl.LOCK_EX
            if not wait or (wait == None and self.wait): flags |= fcntl.LOCK_NB
            fcntl.flock (fd, flags)
            os.write(fd, "%s\n" % os.getpid())
            self.fd = fd
            return True
        except (OSError, IOError), e:
            if fd: os.close(fd)   ## let's not leak  file descriptors
            if e.errno in (errno.EACCES, errno.EAGAIN):
                if (raise_if_fail or (raise_if_fail == None and self.raise_if_fail)):
                    raise LockfileBusyException, self.filename
                else:
                    return False
            else:
                raise

    def release(self):
        '''
        Releases the lock.
        
        If the lock is not held by the current process it does nothing.
        '''
        if not self.fd: return
        try:
            os.unlink(self.filename)
            os.close(self.fd)
        except (OSError, IOError):
            pass

    def __del__(self):
        """
        Avoid stale lock files.
        """
        self.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exception_type, value, traceback):
        self.release()