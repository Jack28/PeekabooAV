###############################################################################
#                                                                             #
# Peekaboo Extended Email Attachment Behavior Observation Owl                 #
#                                                                             #
# sample.py                                                                   #
###############################################################################
#                                                                             #
# Copyright (C) 2016-2019  science + computing ag                             #
#                                                                             #
# This program is free software: you can redistribute it and/or modify        #
# it under the terms of the GNU General Public License as published by        #
# the Free Software Foundation, either version 3 of the License, or (at       #
# your option) any later version.                                             #
#                                                                             #
# This program is distributed in the hope that it will be useful, but         #
# WITHOUT ANY WARRANTY; without even the implied warranty of                  #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU           #
# General Public License for more details.                                    #
#                                                                             #
# You should have received a copy of the GNU General Public License           #
# along with this program.  If not, see <http://www.gnu.org/licenses/>.       #
#                                                                             #
###############################################################################


import os
import hashlib
import re
import errno
import shutil
import logging
import tempfile
from datetime import datetime
from peekaboo.toolbox.sampletools import next_job_hash
from peekaboo.toolbox.files import guess_mime_type_from_file_contents, \
                                   guess_mime_type_from_filename
from peekaboo.toolbox.ms_office import has_office_macros
import peekaboo.ruleset as ruleset


logger = logging.getLogger(__name__)

class SampleFactory(object):
    """ A class for churning out loads of mostly identical sample objects.
    Contains all the global configuration data and object references each
    sample needs and thus serves as a registry of potential API breakage
    perhaps deserving looking into. """
    def __init__(self, cuckoo, connection_map, base_dir, job_hash_regex,
            keep_mail_data):
        # object references for interaction
        self.cuckoo = cuckoo
        self.connection_map = connection_map

        # configuration
        self.base_dir = base_dir
        self.job_hash_regex = job_hash_regex
        self.keep_mail_data = keep_mail_data

    def make_sample(self, file_path, metainfo = {}, socket = None):
        return Sample(file_path, self.cuckoo, metainfo,
                self.connection_map, socket, self.base_dir, self.job_hash_regex,
                self.keep_mail_data)

class Sample(object):
    """
    This class handles and describes samples to be analysed by Peekaboo.

    A sample has attributes like:
    filename, MIME type, sha256, ...
    Those attributes are determined on demand kept in a dictionary, which is
    accessible through the methods has_attr, get_attr, and set_attr.

    The data structure works together with Cuckoo to run behavioral attributes.

    @author: Felix Bauer
    @author: Sebastian Deiss
    """
    def __init__(self, file_path, cuckoo = None, metainfo = {},
            connection_map = None, socket = None, base_dir = None,
            job_hash_regex = None, keep_mail_data = False):
        self.__path = file_path
        self.__cuckoo = cuckoo
        self.__wd = None
        self.__filename = os.path.basename(self.__path)
        # A symlink that points to the actual file named
        # sha256sum.suffix
        self.__submit_path = None
        self.__cuckoo_job_id = -1
        self.__cuckoo_report = None
        self.__result = ruleset.Result.unchecked
        self.__reason = None
        self.__report = []  # Peekaboo's own report
        self.__connection_map = connection_map
        self.__socket = socket
        # Additional attributes for a sample object (e. g. meta info)
        self.__attributes = {}
        self.__base_dir = base_dir
        self.__job_hash_regex = job_hash_regex
        self.__keep_mail_data = keep_mail_data
        self.initialized = False

        # register ourselves with the connection map
        if self.__connection_map is not None and self.__socket is not None:
            self.__connection_map.add(self.__socket, self)

        for field in metainfo:
            logger.debug('meta_info_%s = %s' % (field, metainfo[field]))

            # JSON will transfer null/None values but we don't want them as
            # attributes in that case
            if metainfo[field] is not None:
                self.set_attr('meta_info_' + field, metainfo[field])

    def init(self):
        """
        Initialize the Sample object.

        The actual initialization is done here, because the main thread should
        not do the heavy lifting of e. g. parsing the meta info file to be able
        to accept new connections as quickly as possible.
        Instead, it only adds the sample objects to the queue and the workers
        to the actual initialization.
        """
        if self.initialized:
            return

        logger.debug("initializing sample")

        # create a symlink to submit the file with the correct file extension
        # to cuckoo via submit.py - but only if we can actually figure out an
        # extension. Otherwise the point is moot.
        self.__submit_path = self.__path
        file_ext = self.file_extension
        if file_ext:
            # create a temporary directory where mkdtemp makes sure that
            # creation is atomic, i.e. no other process is using it
            self.__wd = tempfile.mkdtemp(prefix = self.get_job_hash(),
                    dir = self.__base_dir)
            self.__submit_path = os.path.join(self.__wd,
                    '%s.%s' % (self.sha256sum, file_ext))

            logger.debug('ln -s %s %s' % (self.__path, self.__submit_path))
            os.symlink(self.__path, self.__submit_path)

        self.initialized = True

        message = "Datei \"%s\" %s wird analysiert\n" % (self.__filename,
                                                         self.sha256sum)
        self.__report.append(message)
        self.__send_message(message)

        # log some additional info to report to aid debugging
        if self.has_attr('meta_info_name_declared'):
            self.__report.append("meta info: name_declared: %s\n" %
                    self.get_attr('meta_info_name_declared'))

        if self.has_attr('meta_info_type_declared'):
            self.__report.append("meta info: type_declared: %s\n" %
                    self.get_attr('meta_info_type_declared'))

    def get_attr(self, key):
        """
        Get a sample attribute by a specified key.

        @param key: The identifier of the sample attribute to get.
        """
        if self.has_attr(key):
            return self.__attributes[key]
        raise KeyError("Attribute for key '%s' not found." % key)

    def set_attr(self, key, val, override=True):
        """
        Add an attribute to a sample.

        @param key: The identifier of the attribute.
        @param val: The attribute to add.
        @param override: Whether the existing attribute shall be overwritten or not.
        """
        if self.has_attr(key) and override is False:
            raise KeyError("Key '%s' already exists." % key)
        self.__attributes[key] = val

    def has_attr(self, key):
        """
        Check if an attribute exists for this sample.

        @param key: The identifier of the attribute.
        """
        if key in self.__attributes.keys():
            return True
        return False

    def remove_attr(self, key):
        """
        Delete an attribute for this sample.

        @param key: The identifier of the attribute
        @raises ValueError: if the given key was not found in
                            the attributes dictionary.
        """
        if key in self.__attributes.keys():
            del self.__attributes[key]
        raise ValueError('No attribute named "%s" found.' % key)

    def get_file_path(self):
        return self.__path

    def get_filename(self):
        return self.__filename

    def get_result(self):
        return self.__result

    def get_reason(self):
        return self.__reason

    def get_peekaboo_report(self):
        return ''.join(self.__report)

    def get_job_hash(self):
        job_hash = re.sub(self.__job_hash_regex, r'\1',
                          self.__path)
        if job_hash == self.__path:
            # regex did not match.
            # so we generate our own job hash and create the
            # working directory.
            job_hash = next_job_hash()
            os.mkdir(os.path.join(self.__base_dir, job_hash))

        logger.debug("Job hash for this sample: %s" % job_hash)
        return job_hash

    def remove_from_connection_map(self):
        if self.__connection_map is not None:
            # de-register ourselves from the connection map
            if self.__socket is not None:
                self.__connection_map.remove(self.__socket, self)

            if not self.__connection_map.has_connection(self.__socket):
                self.__cleanup_temp_files()
                self.__close_socket()

    def add_rule_result(self, res):
        logger.debug('Adding rule result %s' % str(res))
        rule_results = []
        if self.has_attr('rule_results'):
            rule_results = self.get_attr('rule_results')
        rule_results.append(res)
        self.set_attr('rule_results', rule_results)

    def determine_result(self):
        for rule_result in self.get_attr('rule_results'):
            logger.debug("Current result: %s, Rule result: %s"
                         % (self.__result, rule_result.result))
            # check if result of this rule is worse than what we know so far
            if rule_result.result >= self.__result:
                self.__result = rule_result.result
                self.__reason = rule_result.reason

    def report(self):
        """
        Create the report for this sample. The report is saved as a list of
        strings and is available via get_peekaboo_report(). Also, if a socket connection was
        supplied to the sample the report messages are also written to the socket.
        """
        # TODO: move to rule processing engine.
        self.determine_result()

        for rule_result in self.get_attr('rule_results'):
            message = "Datei \"%s\": %s\n" % (self.__filename, str(rule_result))
            self.__report.append(message)
            self.__send_message(message)

        message = "Die Datei \"%s\" wurde als \"%s\" eingestuft\n\n" \
                  % (self.__filename, self.__result.name)
        self.__report.append(message)
        self.__send_message(message)

    @property
    def sha256sum(self):
        if not self.has_attr('sha256sum'):
            with open(self.__path, 'rb') as f:
                checksum = hashlib.sha256(f.read()).hexdigest()
                self.set_attr('sha256sum', checksum)
                return checksum
        return self.get_attr('sha256sum')

    @property
    def file_extension(self):
        if self.has_attr('file_extension'):
            return self.get_attr('file_extension')

        # try to find a file name containing an extension. Using
        # self.__filename will almost never yield anything useful because
        # amavis intentionally hands us files named only p001, p002 and so on.
        # But we still try it in case there's no declared name.
        filename = self.__filename
        if self.has_attr('meta_info_name_declared'):
            filename = self.get_attr('meta_info_name_declared')

        # extension or the empty string if none found
        file_ext = os.path.splitext(filename)[1][1:]
        self.set_attr('file_extension', file_ext)
        return file_ext

    @property
    def mimetypes(self):
        if self.has_attr('mimetypes'):
            return self.get_attr('mimetypes')

        mime_types = set()

        # get MIME type from meta info
        declared_mt = None
        if self.has_attr('meta_info_type_declared'):
            declared_mt = self.get_attr('meta_info_type_declared')
            if declared_mt is not None:
                logger.debug('Sample declared as "%s"' % declared_mt)
                mime_types.add(declared_mt)

        declared_filename = self.__filename
        if self.has_attr('meta_info_name_declared'):
            declared_filename = self.get_attr('meta_info_name_declared')

        # check if the sample is an S/MIME signature (smime.p7s)
        # If so, don't overwrite the MIME type since we do not want to analyse
        # S/MIME signatures.
        # FIXME: This is oddly specific for this generic routine. Should it be
        # some sort of callback or plugin?
        leave_alone_types = {
            'p7s': [
                'application/pkcs7-signature',
                'application/x-pkcs7-signature',
                'application/pkcs7-mime',
                'application/x-pkcs7-mime',
            ]
        }

        if declared_filename == 'smime.p7s' and declared_mt in leave_alone_types['p7s']:
            logger.info('S/MIME signature detected. Using declared MIME type over detected ones.')
            mime_types = set([declared_mt])
            self.set_attr('mimetypes', mime_types)
            return mime_types

        # determine mime on original p[0-9]* file
        # result of __submit_path would be "inode/symlink"
        content_based_mime_type = guess_mime_type_from_file_contents(self.__path)
        if content_based_mime_type is not None:
            mime_types.add(content_based_mime_type)

        name_based_mime_type = guess_mime_type_from_filename(declared_filename)
        if name_based_mime_type is not None:
            mime_types.add(name_based_mime_type)

        logger.debug('Determined MIME Types: %s' % mime_types)
        self.set_attr('mimetypes', mime_types)
        return mime_types

    @property
    def job_id(self):
        return self.__cuckoo_job_id

    @property
    def office_macros(self):
        if not self.has_attr('office_macros'):
            self.set_attr('office_macros', has_office_macros(self.__path))
        return self.get_attr('office_macros')

    @property
    def file_size(self):
        if not self.has_attr('file_stat'):
            self.set_attr('file_stat', os.stat(self.__path))
        return self.get_attr('file_stat').st_size

    @property
    def analyses_time(self):
        if not self.has_attr('analyses_time'):
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            self.set_attr('analyses_time', timestamp)
            return timestamp
        return self.get_attr('analyses_time')

    @property
    def cuckoo_report(self):
        """ Returns the cuckoo report """
        return self.__cuckoo_report

    def submit_to_cuckoo(self):
        """ Submit the sample to Cuckoo for analysis and record job id.

        @raises: CuckooAnalsisFailedException if submission failed
        @returns: cuckoo job id
        """
        logger.debug("Submitting %s to Cuckoo", self.__submit_path)
        self.__cuckoo_job_id = self.__cuckoo.submit(self.__submit_path)
        message = 'Erfolgreich an Cuckoo gegeben %s als Job %d\n' \
                  % (self, self.__cuckoo_job_id)
        self.__report.append(message)
        return self.__cuckoo_job_id

    def register_cuckoo_report(self, report):
        """ Records a Cuckoo report for later evaluation. """
        self.__cuckoo_report = report

    def __close_socket(self):
        logger.debug('Closing socket connection.')
        try:
            if self.__socket is not None:
                self.__socket.close()
        except EnvironmentError as e:
            # base class for exceptions that can occur outside the Python system.
            # e. g. IOError, OSError
            if e.errno == errno.EPIPE:
                logger.warning('Unable to close the socket. Broken pipe.')
            else:
                logger.exception(e)

    def __cleanup_temp_files(self):
        # nothing to do if we never created a workdir
        if not self.__wd:
            return

        if self.__keep_mail_data:
            logger.debug('Keeping mail data in %s' % self.__wd)
            return

        logger.debug("Deleting tempdir %s" % self.__wd)
        try:
            shutil.rmtree(self.__wd)
        except OSError as e:
            logger.exception(e)

    def __send_message(self, msg):
        """
        Write a message to the socket.

        @param msg: The message to send (max. 1024 bytes).
        """
        if self.__socket is None:
            return
        try:
            self.__socket.send(msg)
            logger.debug('Message send: %s ' % msg)
        except IOError as e:
            if e.errno == errno.EPIPE:
                logger.warning('Unable send message "%s". Broken pipe.' % msg)
            else:
                logger.exception(e)

    def __str__(self):
        return ("<Sample(filename='%s', job_id='%d',"
                " result='%s', sha256sum='%s')>"
                % (self.__filename,
                   self.__cuckoo_job_id,
                   self.__result,
                   self.sha256sum))

    __repr__ = __str__
