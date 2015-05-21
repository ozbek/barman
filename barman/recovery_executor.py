# Copyright (C) 2011-2015 2ndQuadrant Italia (Devise.IT S.r.L.)
#
# This file is part of Barman.
#
# Barman is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Barman is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Barman.  If not, see <http://www.gnu.org/licenses/>.

"""
This module contains the methods necessary to perform a recovery
"""

from io import StringIO
import logging
import os
import re
import shutil
import tempfile
import time

import collections
import dateutil.parser
import dateutil.tz

from barman import xlog, output
from barman.command_wrappers import DataTransferFailure, \
    CommandFailedException, RsyncPgData, Rsync
from barman.fs import FsOperationFailed, UnixRemoteCommand, UnixLocalCommand
from barman.infofile import BackupInfo
from barman.utils import mkpath


# generic logger for this module
_logger = logging.getLogger(__name__)

# regexp matching a single value in Postgres configuration file
PG_CONF_SETTING_RE = re.compile(r"^\s*([^\s=]+)\s*=\s*(.*)$")

# create a namedtuple object called Assertion with 'filename', 'line', 'key' and
# 'value' as properties
Assertion = collections.namedtuple('Assertion', 'filename line key value')


# noinspection PyMethodMayBeStatic
class RecoveryExecutor(object):
    """
    Class responsible of recovery operations
    """

    # Potentially dangerous options list, which need to be revised by the user
    # after a recovery
    DANGEROUS_OPTIONS = ['data_directory', 'config_file', 'hba_file',
                         'ident_file', 'external_pid_file', 'ssl_cert_file',
                         'ssl_key_file', 'ssl_ca_file', 'ssl_crl_file',
                         'unix_socket_directory']

    # List of options that, if present, need to be forced to a specific value
    # during recovery, to avoid data losses
    MANGLE_OPTIONS = {'archive_command': 'false'}

    def __init__(self, backup_manager):
        """
        Constructor

        :param barman.backup.BackupManager backup_manager: the BackupManager
            owner of the executor
        """
        self.backup_manager = backup_manager
        self.server = backup_manager.server
        self.config = backup_manager.config

    def pg_config_mangle(self, filename, settings, backup_filename=None):
        """
        This method modifies the given PostgreSQL configuration file,
        commenting out the given settings, and adding the ones generated by
        Barman.

        If backup_filename is passed, performs a backup copy first.

        :param filename: the PostgreSQL configuration file
        :param settings: dictionary of settings to be mangled
        :param backup_filename: config file backup copy. Default is None.
        """
        if backup_filename:
            shutil.copy2(filename, backup_filename)

        with open(filename) as f:
            content = f.readlines()

        mangled = []
        with open(filename, 'w') as f:
            for l_number, line in enumerate(content):
                rm = PG_CONF_SETTING_RE.match(line)
                if rm:
                    key = rm.group(1)
                    if key in settings:
                        f.write("#BARMAN# %s" % line)
                        # TODO is it useful to handle none values?
                        changes = "%s = %s\n" % (key, settings[key])
                        f.write(changes)
                        mangled.append(
                            Assertion._make([
                                os.path.basename(f.name),
                                l_number,
                                key,
                                settings[key]]))
                        continue
                f.write(line)

        return mangled

    def pg_config_detect_possible_issues(self, filename):
        """
        This method looks for any possible issue with PostgreSQL
        location options such as data_directory, config_file, etc.
        It returns a dictionary with the dangerous options that have been found.

        :param filename: the Postgres configuration file
        """

        clashes = []

        with open(filename) as f:
            content = f.readlines()

        # Read line by line and identify dangerous options
        for l_number, line in enumerate(content):
            rm = PG_CONF_SETTING_RE.match(line)
            if rm:
                key = rm.group(1)
                if key in self.DANGEROUS_OPTIONS:
                    clashes.append(
                        Assertion._make([
                            os.path.basename(f.name),
                            l_number,
                            key,
                            rm.group(2)]))

        return clashes

    def map_temporary_config_files(self, recovery_info, backup_info,
                                   remote_command):
        """
        Map configuration files, by filling the 'temporary_configuration_files'
        array, depending on remote or local recovery. This array will be used by
        the subsequent methods of the class.

        :param dict recovery_info: Dictionary containing all the recovery params
        :param barman.infofile.BackupInfo backup_info: a backup representation
        :param str remote_command: ssh command for remote recovery
        """
        for conf_file in recovery_info['configuration_files']:
            if remote_command:
                # If the recovery is remote, copy the postgresql.conf
                # file in a temp dir
                # Otherwise we can modify the postgresql.conf file
                # in the destination directory.
                conf_file_path = os.path.join(
                    recovery_info['tempdir'], conf_file)
                shutil.copy2(
                    os.path.join(backup_info.get_data_directory(),
                                 conf_file), conf_file_path)
            # If is a remote recovery the conf files are inside a temporary dir
            else:
                # Otherwise use the local destination path.
                conf_file_path = os.path.join(recovery_info['destination_path'],
                                              conf_file)
            recovery_info['temporary_configuration_files'].append(
                conf_file_path)

    def analyse_temporary_config_files(self, recovery_info):
        """
        Analyse temporary configuration files and identify dangerous options

        Mark all the dangerous options for the user to review. This procedure
        also changes harmful options such as 'archive_command'.

        :param dict recovery_info: dictionary holding all recovery parameters
        """
        results = recovery_info['results']
        # Check for dangerous options inside every config file
        for conf_file in recovery_info['temporary_configuration_files']:

            # Identify and comment out dangerous options, replacing them with
            # the appropriate values
            results['changes'] += self.pg_config_mangle(
                conf_file,
                self.MANGLE_OPTIONS,
                "%s.origin" % conf_file)
            # Identify dangerous options and warn users about their presence
            results['warnings'] += self.pg_config_detect_possible_issues(
                conf_file)

    def copy_temporary_config_files(self, dest, remote_command, recovery_info):
        """
        Copy modified configuration files using rsync in case of remote recovery

        :param str dest: destination directory of the recovery
        :param str remote_command: ssh command for remote connection
        :param dict recovery_info: Dictionary containing all the recovery params
        """
        if remote_command:
            # If this is a remote recovery, rsync the modified files from the
            # temporary local directory to the remote destination directory.
            file_list = []
            for conf_file in recovery_info['configuration_files']:
                file_list.append('%s' % conf_file)
                file_list.append('%s.origin' % conf_file)

            try:
                recovery_info['rsync'].from_file_list(file_list,
                                                      recovery_info['tempdir'],
                                                      ':%s' % dest)
            except CommandFailedException, e:
                output.exception(
                    'remote copy of configuration files failed: %s', e)
                output.close_and_exit()

    def prepare_tablespaces(self, backup_info, cmd, dest, tablespaces):
        """
        Prepare the directory structure for required tablespaces, taking care of
        tablespaces relocation, if requested.

        :param barman.infofile.BackupInfo backup_info: backup representation
        :param barman.fs.UnixLocalCommand cmd: Object for filesystem interaction
        :param str dest: destination dir for the recovery
        :param dict tablespaces: dict of all the tablespaces and their location
        """
        tblspc_dir = os.path.join(dest, 'pg_tblspc')
        try:
            # check for pg_tblspc dir into recovery destination folder.
            # if it does not exists, create it
            cmd.create_dir_if_not_exists(tblspc_dir)
        except FsOperationFailed, e:
            output.exception("unable to initialise tablespace directory "
                             "'%s': %s", tblspc_dir, e)
            output.close_and_exit()
        for item in backup_info.tablespaces:

            # build the filename of the link under pg_tblspc directory
            pg_tblspc_file = os.path.join(tblspc_dir, str(item.oid))

            # by default a tablespace goes in the same location where
            # it was on the source server when the backup was taken
            location = item.location

            # if a relocation has been requested for this tablespace,
            # use the target directory provided by the user
            if tablespaces and item.name in tablespaces:
                location = tablespaces[item.name]

            try:
                # remove the current link in pg_tblspc, if it exists
                # (raise an exception if it is a directory)
                cmd.delete_if_exists(pg_tblspc_file)
                # create tablespace location, if does not exist
                # (raise an exception if it is not possible)
                cmd.create_dir_if_not_exists(location)
                # check for write permissions on destination directory
                cmd.check_write_permission(location)
                # create symlink between tablespace and recovery folder
                cmd.create_symbolic_link(location, pg_tblspc_file)
            except FsOperationFailed, e:
                output.exception("unable to prepare '%s' tablespace "
                                 "(destination '%s'): %s",
                                 item.name, location, e)
                output.close_and_exit()
            output.info("\t%s, %s, %s", item.oid, item.name, location)

    def retrieve_safe_horizon(self, recovery_info, backup_info, dest):
        """
        Retrieve the safe_horizon for smart copy

        If the target directory contains a previous recovery, it is safe to
        pick the least of the two backup "begin times" (the one we are
        recovering now and the one previously recovered in the target
        directory). Set the value in the given recovery_info dictionary.

        :param dict recovery_info: Dictionary containing all the recovery params
        :param barman.infofile.BackupInfo backup_info: a backup representation
        :param str dest: recovery destination directory
        """
        # noinspection PyBroadException
        try:
            backup_begin_time = backup_info.begin_time
            # Retrieve previously recovered backup metadata (if available)
            dest_info_txt = recovery_info['cmd'].get_file_content(
                os.path.join(dest, '.barman-recover.info'))
            dest_info = BackupInfo(
                self.server,
                info_file=StringIO(dest_info_txt))
            dest_begin_time = dest_info.begin_time
            # Pick the earlier begin time. Both are tz-aware timestamps because
            # BackupInfo class ensure it
            safe_horizon = min(backup_begin_time, dest_begin_time)
            output.info("Using safe horizon time for smart rsync copy: %s",
                        safe_horizon)
        except FsOperationFailed, e:
            # Setting safe_horizon to None will effectively disable
            # the time-based part of smart_copy method. However it is still
            # faster than running all the transfers with checksum enabled.
            #
            # FsOperationFailed means the .barman-recover.info is not available
            # on destination directory
            safe_horizon = None
            _logger.warning('Unable to retrieve safe horizon time '
                            'for smart rsync copy: %s', e)
        except Exception, e:
            # Same as above, but something failed decoding .barman-recover.info
            # or comparing times, so log the full traceback
            safe_horizon = None
            _logger.exception('Error retrieving safe horizon time '
                              'for smart rsync copy: %s', e)

        recovery_info['safe_horizon'] = safe_horizon

    def generate_recovery_conf(self, recovery_info, backup_info, dest,
                               exclusive, remote_command, target_name,
                               target_time, target_tli, target_xid):
        """
        Generate a recovery.conf file for PITR containing
        all the required configurations

        :param dict recovery_info: Dictionary containing all the recovery params
        :param barman.infofile.BackupInfo backup_info: representation of a
            backup
        :param str dest: destination directory of the recovery
        :param boolean exclusive: exclusive backup or concurrent
        :param str remote_command: ssh command for remote connection
        :param str target_name: recovery target name for PITR
        :param str target_time: recovery target time for PITR
        :param str target_tli: recovery target timeline for PITR
        :param str target_xid: recovery target transaction id for PITR
        """
        if remote_command:
            recovery = open(os.path.join(recovery_info['tempdir'],
                                         'recovery.conf'), 'w')
        else:
            recovery = open(os.path.join(dest, 'recovery.conf'), 'w')
        print >> recovery, "restore_command = 'cp barman_xlog/%f %p'"
        if backup_info.version >= 80400:
            print >> recovery, "recovery_end_command = 'rm -fr barman_xlog'"
        if target_time:
            print >> recovery, "recovery_target_time = '%s'" % target_time
        if target_tli:
            print >> recovery, "recovery_target_timeline = %s" % target_tli
        if target_xid:
            print >> recovery, "recovery_target_xid = '%s'" % target_xid
        if target_name:
            print >> recovery, "recovery_target_name = '%s'" % target_name
        if (target_xid or target_time) and exclusive:
            print >> recovery, "recovery_target_inclusive = '%s'" % (
                not exclusive)
        recovery.close()
        if remote_command:
            # Uses plain rsync (without exclusions) to ship recovery.conf
            plain_rsync = Rsync(
                ssh=remote_command,
                bwlimit=self.config.bandwidth_limit,
                network_compression=self.config.network_compression)
            try:
                plain_rsync.from_file_list(['recovery.conf'],
                                           recovery_info['tempdir'],
                                           ':%s' % dest)
            except CommandFailedException, e:
                output.exception(
                    'remote copy of recovery.conf failed: %s', e)
                output.close_and_exit()

    def generate_archive_status(self, recovery_info, remote_command,
                                required_xlog_files):
        """
        Populate the archive_status directory

        :param dict recovery_info: Dictionary containing all the recovery params
        :param str remote_command: ssh command for remote connection
        :param tuple required_xlog_files: list of required WAL segments
        """
        if remote_command:
            status_dir = recovery_info['tempdir']
        else:
            status_dir = os.path.join(recovery_info['wal_dest'],
                                      'archive_status')
            mkpath(status_dir)
        for wal_info in required_xlog_files:
            with open(os.path.join(status_dir, "%s.done" % wal_info.name),
                      'a') as f:
                f.write('')
        if remote_command:
            try:
                recovery_info['rsync']('%s/' % status_dir,
                                       ':%s' % os.path.join(
                                           recovery_info['wal_dest'],
                                           'archive_status'))
            except CommandFailedException as e:
                output.exception(
                    "unable to populate pg_xlog/archive_status"
                    "directory: %s", e)
                output.close_and_exit()

    def setup(self, backup_info, remote_command, dest):
        """
        Prepare the recovery_info dictionary for the recovery, as well
        as temporary working directory

        :param barman.infofile.BackupInfo backup_info: representation of a
            backup
        :param str remote_command: ssh command for remote connection
        :return dict: recovery_info dictionary, holding the basic values for a
            recovery
        """
        recovery_info = {
            'cmd': None,
            'recovery_dest': 'local',
            'rsync': None,
            'configuration_files': [],
            'destination_path': dest,
            'temporary_configuration_files': [],
            'tempdir': tempfile.mkdtemp(prefix='barman_recovery-'),
            'is_pitr': False,
            'wal_dest': os.path.join(dest, 'pg_xlog')}
        # A map that will keep track of the results of the recovery.
        # Used for output generation
        results = {
            'changes': [],
            'warnings': [],
            'delete_barman_xlog': False
        }
        recovery_info['results'] = results

        # Set up a list of configuration files
        recovery_info['configuration_files'].append('postgresql.conf')
        if backup_info.version >= 90400:
            recovery_info['configuration_files'].append('postgresql.auto.conf')

        # Handle remote recovery options
        if remote_command:
            recovery_info['recovery_dest'] = 'remote'
            recovery_info['rsync'] = RsyncPgData(
                ssh=remote_command,
                bwlimit=self.config.bandwidth_limit,
                network_compression=self.config.network_compression)
            try:
                # create a UnixRemoteCommand obj if is a remote recovery
                recovery_info['cmd'] = UnixRemoteCommand(remote_command)
            except FsOperationFailed:
                output.error(
                    "Unable to connect to the target host using the command "
                    "'%s'", remote_command)
                output.close_and_exit()
        else:
            # if is a local recovery create a UnixLocalCommand
            recovery_info['cmd'] = UnixLocalCommand()

        return recovery_info

    def teardown(self, recovery_info):
        """
        Cleanup operations for a recovery

        :param dict recovery_info: dictionary holding the basic values
            for a recovery
        """
        # Remove the temporary directory (created in the setup method)
        shutil.rmtree(recovery_info['tempdir'])

    def set_pitr_targets(self, recovery_info, backup_info, dest, target_name,
                         target_time, target_tli, target_xid):
        """
        Set PITR targets - as specified by the user

        :param dict recovery_info: Dictionary containing all the recovery params
        :param barman.infofile.BackupInfo backup_info: representation of a
            backup
        :param str dest: destination directory of the recovery
        :param str|None target_name: recovery target name for PITR
        :param str|None target_time: recovery target time for PITR
        :param str|None target_tli: recovery target timeline for PITR
        :param str|None target_xid: recovery target transaction id for PITR
        """
        target_epoch = None
        target_datetime = None
        if (target_time or
                target_xid or
                (target_tli and target_tli != backup_info.timeline) or
                target_name):
            recovery_info['is_pitr'] = True
            targets = {}
            if target_time:
                # noinspection PyBroadException
                try:
                    target_datetime = dateutil.parser.parse(target_time)
                except ValueError as e:
                    output.exception(
                        "unable to parse the target time parameter %r: %s",
                        target_time, e)
                    output.close_and_exit()
                except Exception:
                    # this should not happen, but there is a known bug in
                    # dateutil.parser.parse() implementation
                    # ref: https://bugs.launchpad.net/dateutil/+bug/1247643
                    output.exception(
                        "unable to parse the target time parameter %r",
                        target_time)
                    output.close_and_exit()

                target_epoch = (
                    time.mktime(target_datetime.timetuple()) +
                    (target_datetime.microsecond / 1000000.))
                targets['time'] = str(target_datetime)
            if target_xid:
                targets['xid'] = str(target_xid)
            if target_tli and target_tli != backup_info.timeline:
                targets['timeline'] = str(target_tli)
            if target_name:
                targets['name'] = str(target_name)
            output.info(
                "Doing PITR. Recovery target %s",
                (", ".join(["%s: %r" % (k, v) for k, v in targets.items()])))
            recovery_info['wal_dest'] = os.path.join(dest, 'barman_xlog')

            # With a PostgreSQL version older than 8.4, it is the user's
            # responsibility to delete the "barman_xlog" directory as the
            # restore_command option in recovery.conf is not supported
            if backup_info.version < 80400:
                recovery_info['results']['delete_barman_xlog'] = True
        recovery_info['target_epoch'] = target_epoch
        recovery_info['target_datetime'] = target_datetime

    def recover(self, backup_info, dest, tablespaces, target_tli,
                target_time, target_xid, target_name,
                exclusive, remote_command):
        """
        Performs a recovery of a backup

        :param barman.infofile.BackupInfo backup_info: the backup to recover
        :param str dest: the destination directory
        :param dict[str,str]|None tablespaces: a tablespace name -> location map
            (for relocation)
        :param str|None target_tli: the target timeline
        :param str|None target_time: the target time
        :param str|None target_xid: the target xid
        :param str|None target_name: the target name created previously with
                            pg_create_restore_point() function call
        :param bool exclusive: whether the recovery is exclusive or not
        :param str|None remote_command: The remote command to recover
                               the base backup, in case of remote backup.
        """

        # Run the cron to be sure the wal catalog is up to date
        # Prepare a map that contains all the objects required for a recovery
        recovery_info = self.setup(backup_info, remote_command, dest)
        output.info("Starting %s restore for server %s using backup %s",
                    recovery_info['recovery_dest'], self.server.config.name,
                    backup_info.backup_id)
        output.info("Destination directory: %s", dest)

        # Set targets for PITR
        self.set_pitr_targets(recovery_info,
                              backup_info, dest,
                              target_name,
                              target_time,
                              target_tli,
                              target_xid)

        # Retrieve the safe_horizon for smart copy
        self.retrieve_safe_horizon(recovery_info, backup_info, dest)

        # check destination directory. If doesn't exist create it
        try:
            recovery_info['cmd'].create_dir_if_not_exists(dest)
        except FsOperationFailed, e:
            output.exception("unable to initialise destination directory "
                             "'%s': %s", dest, e)
            output.close_and_exit()

        # Initialize tablespace directories
        if backup_info.tablespaces:
            self.prepare_tablespaces(backup_info,
                                     recovery_info['cmd'],
                                     dest,
                                     tablespaces)
        # Copy the base backup
        output.info("Copying the base backup.")
        try:
            # perform the backup copy, honoring the retry option if set
            self.backup_manager.retry_backup_copy(
                self.basebackup_copy,
                backup_info, dest,
                tablespaces, remote_command,
                recovery_info['safe_horizon'])
        except DataTransferFailure, e:
            output.exception("Failure copying base backup: %s", e)
            output.close_and_exit()

        # Copy the backup.info file in the destination as ".barman-recover.info"
        if remote_command:
            try:
                recovery_info['rsync'](backup_info.filename,
                                       ':%s/.barman-recover.info' % dest)
            except CommandFailedException, e:
                output.exception(
                    'copy of recovery metadata file failed: %s', e)
                output.close_and_exit()
        else:
            backup_info.save(os.path.join(dest, '.barman-recover.info'))

        # Prepare WAL segments local directory
        output.info("Copying required WAL segments.")

        required_xlog_files = tuple(
            self.server.get_required_xlog_files(backup_info,
                                                target_tli,
                                                recovery_info['target_epoch']))

        # Restore WAL segments into the wal_dest directory
        try:
            self.xlog_copy(required_xlog_files,
                           recovery_info['wal_dest'],
                           remote_command)
        except DataTransferFailure, e:
            output.exception("Failure copying WAL files: %s", e)
            output.close_and_exit()

        # Generate recovery.conf file (only if needed by PITR)
        if recovery_info['is_pitr']:
            output.info("Generating recovery.conf")
            self.generate_recovery_conf(recovery_info, backup_info, dest,
                                        exclusive, remote_command, target_name,
                                        target_time, target_tli, target_xid)
        else:
            # avoid shipping of just recovered pg_xlog files
            output.info("Generating archive status files")
            self.generate_archive_status(recovery_info, remote_command,
                                         required_xlog_files)

        # As last step, analyse configuration files in order to spot
        # harmful options. Barman performs automatic conversion of
        # some options as well as notifying users of their existence.
        #
        # This operation is performed in three steps:
        # 1) mapping
        # 2) analysis
        # 3) copy
        output.info("Identify dangerous settings in destination directory.")

        self.map_temporary_config_files(recovery_info,
                                        backup_info,
                                        remote_command)
        self.analyse_temporary_config_files(recovery_info)
        self.copy_temporary_config_files(dest,
                                         remote_command,
                                         recovery_info)

        # Cleanup operations
        self.teardown(recovery_info)

        return recovery_info

    def basebackup_copy(self, backup_info, dest, tablespaces=None,
                        remote_command=None, safe_horizon=None):
        """
        Perform the actual copy of the base backup for recovery purposes

        :param barman.infofile.BackupInfo backup_info: the backup to recover
        :param str dest: the destination directory
        :param dict[str,str]|None tablespaces: a tablespace name -> location map
            (for relocation)
        :param str|None remote_command: default None. The remote command to
            recover the base backup, in case of remote backup.
        :param datetime.datetime|None safe_horizon: anything after this time
            has to be checked with checksum
        """

        # Dictionary for paths to be excluded from rsync
        exclude_and_protect = []

        # Set a ':' prefix to remote destinations
        dest_prefix = ''
        if remote_command:
            dest_prefix = ':'

        # Copy tablespaces applying bwlimit when necessary
        if backup_info.tablespaces:
            tablespaces_bw_limit = self.config.tablespace_bandwidth_limit
            # Copy a tablespace at a time
            for tablespace in backup_info.tablespaces:
                # Apply bandwidth limit if requested
                bwlimit = self.config.bandwidth_limit
                if tablespaces_bw_limit and \
                        tablespace.name in tablespaces_bw_limit:
                    bwlimit = tablespaces_bw_limit[tablespace.name]
                # By default a tablespace goes in the same location where
                # it was on the source server when the backup was taken
                location = tablespace.location
                # If a relocation has been requested for this tablespace
                # use the user provided target directory
                if tablespaces and tablespace.name in tablespaces:
                    location = tablespaces[tablespace.name]
                # If the tablespace location is inside the data directory,
                # exclude and protect it from being deleted during
                # the data directory copy
                if location.startswith(dest):
                    exclude_and_protect.append(location[len(dest):])
                # Exclude and protect the tablespace from being deleted during
                # the data directory copy
                exclude_and_protect.append("/pg_tblspc/%s" % tablespace.oid)
                # Copy the tablespace using smart copy
                tb_rsync = RsyncPgData(
                    ssh=remote_command,
                    bwlimit=bwlimit,
                    network_compression=self.config.network_compression,
                    check=True)
                try:
                    tb_rsync.smart_copy(
                        '%s/' % backup_info.get_data_directory(tablespace.oid),
                        dest_prefix + location,
                        safe_horizon)
                except CommandFailedException, e:
                    msg = "data transfer failure on directory '%s'" % location
                    raise DataTransferFailure.from_rsync_error(e, msg)

        # Copy the pgdata directory
        rsync = RsyncPgData(
            ssh=remote_command,
            bwlimit=self.config.bandwidth_limit,
            exclude_and_protect=exclude_and_protect,
            network_compression=self.config.network_compression)
        try:
            rsync.smart_copy(
                '%s/' % backup_info.get_data_directory(),
                dest_prefix + dest,
                safe_horizon)
        except CommandFailedException, e:
            msg = "data transfer failure on directory '%s'" % dest
            raise DataTransferFailure.from_rsync_error(e, msg)

            # TODO: Manage different location for configuration files
            # TODO: that were not within the data directory

    def xlog_copy(self, required_xlog_files, wal_dest, remote_command):
        """
        Restore WAL segments

        :param required_xlog_files: list of all required WAL files
        :param wal_dest: the destination directory for xlog recover
        :param remote_command: default None. The remote command to recover
               the xlog, in case of remote backup.
        """
        # Retrieve the list of required WAL segments
        # according to recovery options
        xlogs = {}
        for wal_info in required_xlog_files:
            hashdir = xlog.hash_dir(wal_info.name)
            if hashdir not in xlogs:
                xlogs[hashdir] = []
            xlogs[hashdir].append(wal_info.name)
        # Check decompression options
        compressor = self.backup_manager.compression_manager.get_compressor()

        rsync = RsyncPgData(
            ssh=remote_command,
            bwlimit=self.config.bandwidth_limit,
            network_compression=self.config.network_compression)
        if remote_command:
            # If remote recovery tell rsync to copy them remotely
            # add ':' prefix to mark it as remote
            # add '/' suffix to ensure it is a directory
            wal_dest = ':%s/' % wal_dest
        else:
            # we will not use rsync: destdir must exists
            mkpath(wal_dest)
        if compressor and remote_command:
            xlog_spool = tempfile.mkdtemp(prefix='barman_xlog-')
        total_wals = sum(map(len, xlogs.values()))
        partial_count = 0
        for prefix in sorted(xlogs):
            batch_len = len(xlogs[prefix])
            partial_count += batch_len
            source_dir = os.path.join(self.config.wals_directory, prefix)
            _logger.info(
                "Starting copy of %s WAL files %s/%s from %s to %s",
                batch_len,
                partial_count,
                total_wals,
                xlogs[prefix][0],
                xlogs[prefix][-1])
            if compressor:
                if remote_command:
                    for segment in xlogs[prefix]:
                        compressor.decompress(os.path.join(source_dir, segment),
                                              os.path.join(xlog_spool, segment))
                    try:
                        rsync.from_file_list(xlogs[prefix],
                                             xlog_spool, wal_dest)
                    except CommandFailedException, e:
                        msg = "data transfer failure while copying WAL files " \
                              "to directory '%s'" % (wal_dest[1:],)
                        raise DataTransferFailure.from_rsync_error(e, msg)

                    # Cleanup files after the transfer
                    for segment in xlogs[prefix]:
                        file_name = os.path.join(xlog_spool, segment)
                        try:
                            os.unlink(file_name)
                        except OSError as e:
                            output.warning(
                                "Error removing temporary file '%s': %s",
                                file_name, e)
                else:
                    # decompress directly to the right place
                    for segment in xlogs[prefix]:
                        compressor.decompress(os.path.join(source_dir, segment),
                                              os.path.join(wal_dest, segment))
            else:
                try:
                    rsync.from_file_list(
                        xlogs[prefix],
                        "%s/" % os.path.join(
                            self.config.wals_directory, prefix),
                        wal_dest)
                except CommandFailedException, e:
                    msg = "data transfer failure while copying WAL files " \
                          "to directory '%s'" % (wal_dest[1:],)
                    raise DataTransferFailure.from_rsync_error(e, msg)

        _logger.info("Finished copying %s WAL files.", total_wals)

        if compressor and remote_command:
            shutil.rmtree(xlog_spool)
