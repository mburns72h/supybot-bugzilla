###
# Copyright (c) 2007, Max Kanat-Alexander
# All rights reserved.
#
#
###

import supybot.utils as utils
from supybot.utils.structures import TimeoutQueue
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.registry as registry
import supybot.schedule as schedule
import supybot.callbacks as callbacks
import supybot.plugins.Web.plugin as Web

import re
import urllib
import xml.dom.minidom as minidom

import bugmail
import mailbox
import email
from time import time
import os
import errno
try:
    import fcntl
except ImportError:
    fcntl = None

'''The maximum amount of time that the bugmail poller will wait
   for a dotlock to be released, in seconds, before throwing an
   exception.'''
MAX_DOTLOCK_WAIT = 300

'''For attachment.cgi in edit mode, how many bytes, starting at the
   beginning of the page, should we search through to get the title?'''
ATTACH_TITLE_SIZE = 512

######################################
# Utility Functions for Mbox Polling #
###################################### 

def _lock_file(f):
    """Lock file f using lockf and dot locking."""
    # XXX This seems to be causing problems in directories that we don't own.
    return
    dotlock_done = False
    try:
        if fcntl:
            fcntl.lockf(f, fcntl.LOCK_EX)

        pre_lock = _create_temporary(f.name + '.lock')
        pre_lock.close()

        start_dotlock = time()
        while (not dotlock_done):
            try:
                if hasattr(os, 'link'):
                    os.link(pre_lock.name, f.name + '.lock')
                    dotlock_done = True
                    os.unlink(pre_lock.name)
                else:
                    os.rename(pre_lock.name, f.name + '.lock')
                    dotlock_done = True
            except OSError, e:
                if e.errno != errno.EEXIST: raise

            if time() - start_dotlock > MAX_DOTLOCK_WAIT:
                raise IOError, 'Timed-out while waiting for dot-lock'

    except:
        if fcntl:
            fcntl.lockf(f, fcntl.LOCK_UN)
        if dotlock_done:
            os.remove(f.name + '.lock')
        raise

def _create_temporary(path):
    """Create a temp file based on path and open for reading and writing."""
    file_name = '%s.%s.%s' % (path, int(time()), os.getpid())
    fd = os.open(file_name, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    try:
        return open(file_name, 'rb+')
    finally:
        os.close(fd)

def _unlock_file(f):
    """Unlock file f using lockf and dot locking."""
    if fcntl:
        fcntl.lockf(f, fcntl.LOCK_UN)
    if os.path.exists(f.name + '.lock'):
        os.remove(f.name + '.lock')

def _message_factory(fp):
    try:
        return email.message_from_file(fp)
    except email.Errors.MessageParseError:
        # Don't return None since that will
        # stop the mailbox iterator
        return ''

def _getTagText(bug, field):
    # XXX This should probably support multiplicable fields
    node = bug.getElementsByTagName(field)
    node_text = None
    if node:
        node_text = _getXmlText(node)
        # Include Resolution in status
        if field == 'bug_status':
            res_node = bug.getElementsByTagName('resolution')
            if res_node:
                node_text += ' ' + _getXmlText(res_node)
    return node_text

def _getXmlText(node):
    return node[0].childNodes[0].data

class Bugzilla(callbacks.PluginRegexp):
    """This plugin provides the ability to interact with Bugzilla installs.
    It can report changes from your Bugzilla by parsing emails, and it can
    report the details of bugs and attachments to your channel."""

    threaded = True
    callBefore = ['URL', 'Web']
    regexps = ['snarfBugUrl']
    unaddressedRegexps = ['snarfBug']

    '''Words that describe each flag status except "requested."'''
    status_words = { '+' : 'granted', '-' : 'denied', 
                     'cancelled' : 'cancelled' }

    def __init__(self, irc):
        self.__parent = super(Bugzilla, self)
        self.__parent.__init__(irc)
        self.saidBugs = ircutils.IrcDict()
        self.saidAttachments = ircutils.IrcDict()
        sayTimeout = self.registryValue('bugSnarferTimeout')
        for k in irc.state.channels.keys():
            self.saidBugs[k] = TimeoutQueue(sayTimeout)
            self.saidAttachments[k] = TimeoutQueue(sayTimeout)
        period = self.registryValue('mboxPollTimeout')
        schedule.addPeriodicEvent(self._pollMbox, period, name=self.name())

    def die(self):
        self.__parent.die()
        schedule.removeEvent(self.name())

    def attachment(self, irc, msg, args, attach_ids):
        """<attach_id> [<attach_id>]+
        Reports the details of the attachment with that id to this channel.
        Accepts a space-separated list of ids if you want to report the details
        of more than one attachment."""

        channel = msg.args[0]
        url = self.registryValue('bugzilla', channel)
        lines = self._getAttachments(url, attach_ids, channel)
        for l in lines: irc.reply(l)
    attachment = wrap(attachment, [many(('id','attachment'))])

    def bug(self, irc, msg, args, bug_id_string):
        """<bug_id> [<bug_ids>]
        Reports the details of the bugs with the listed ids to this channel.
        Accepts bug aliases as well as numeric ids. Your list can be separated
        by spaces, commas, and the word "and" if you want."""

        channel = msg.args[0]
        bug_ids = re.split('[!?.,\(\)\s]|[\b\W]and[\b\W]*|\bbug\b', 
                           bug_id_string)
        url = self.registryValue('bugzilla', channel)
        self.log.debug('Getting bug_ids %s from %s' % (', '.join(bug_ids), url))
        bug_strings = self._getBugs(url, bug_ids, channel)
        for s in bug_strings:
            irc.reply(s)
    bug = wrap(bug, ['text'])

    def query(self, irc, msg, args, options, query_string):
        """[--total] <search terms>
        Searches your Bugzilla using the QuickSearch syntax, and returns
        a certain number of results. If you specify --total, it will return
        the total number of results found, instead of the actual results."""

        channel = msg.args[0]
        total = ('total', True) in options
        url = self.registryValue('bugzilla', channel)

        # Build the query URL
        full_query = query_string + ' ' +  self.registryValue('queryTerms', channel)
        queryurl = '%s/buglist.cgi?quicksearch=%s&ctype=csv&columnlist=bug_id' \
                   % (url, urllib.quote(full_query))
        if not total:
            queryurl = '%s&limit=%d' \
                % (queryurl, self.registryValue('queryResultLimit', channel))

        self.log.debug('QuickSearch: ' + queryurl)
        bug_csv = utils.web.getUrl(queryurl)
        if not bug_csv:
             raise callbacks.Error, 'Got empty CSV'

        if bug_csv.find('DOCTYPE') == -1:
            bug_ids = bug_csv.split("\n")
            del bug_ids[0] # Removes the "bug_id" header.
        else:
            # Searching a bug alias will return just that bug.
            bug_ids = [query_string]

        if not bug_ids:
            irc.reply('No results for "%s."' % query_string)
            return

        if total:
            irc.reply('%d results for "%s."' % (len(bug_ids), query_string))
        else:
            bug_strings = self._getBugs(url, bug_ids, channel)
            for s in bug_strings:
                irc.reply(s)
    query = wrap(query, [getopts({'total' : ''}), 'text'])

    def snarfBug(self, irc, msg, match):
        r"""\b(?P<type>bug|attachment)\b[\s#]*(?P<id>\d+)"""
        channel = msg.args[0]
        if not self.registryValue('bugSnarfer', channel): return

        id_matches = match.group('id').split()
        type = match.group('type')
        ids = []
        self.log.debug('Snarfed ID(s): ' + ' '.join(id_matches))
        # Check if the bug has been already snarfed in the last X seconds
        for id in id_matches:
            if type.lower() == 'bug': 
                should_say = self._shouldSayBug(id, channel)
            else: 
                should_say = self._shouldSayAttachment(id, channel)
             
            if should_say:
                ids.append(id)
        if not ids: return

        url = self.registryValue('bugzilla', channel)
        if type.lower() == 'bug': 
            strings = self._getBugs(url, ids, channel)
        else: 
            strings = self._getAttachments(url, ids, channel)

        for s in strings:
            irc.reply(s, prefixNick=False)

    def snarfBugUrl(self, irc, msg, match):
        r"(https?://\S+)/show_bug.cgi\?id=(?P<bug>\w+)"
        channel = msg.args[0]
        if (not self.registryValue('bugSnarfer', channel)): return

        bug_ids =  match.group('bug').split()
        self.log.debug('Snarfed Bug IDs: ' + ' '.join(bug_ids))
        url = self.registryValue('bugzilla', channel)
        bug_strings = self._getBugs(url, bug_ids, channel, show_url=False)
        for s in bug_strings:
            irc.reply(s, prefixNick=False)

    def _formatLine(self, line, channel, type):
        """Implements the 'format' configuration options."""
        format = self.registryValue('format.%s' % type, channel)
        already_colored = False
        for item in format:
            if item == 'bold':
                line = ircutils.bold(line)
            elif item == 'reverse':
                line = ircutils.reverse(line)
            elif item == 'underlined':
                line = ircutils.underline(line)
            elif already_colored:
                line = ircutils.mircColor(line, bg=item)
            else:
                line = ircutils.mircColor(line, fg=item)
        return line

    def _shouldSayBug(self, bug_id, channel):
        if channel not in self.saidBugs:
            sayTimeout = self.registryValue('bugSnarferTimeout')
            self.saidBugs[channel] = TimeoutQueue(sayTimeout)
        if bug_id in self.saidBugs[channel]:
            return False

        self.saidBugs[channel].enqueue(bug_id)
        #self.log.debug('After checking bug %s queue is %r' \
        #                % (bug_id, self.saidBugs[channel]))
        return True

    def _shouldSayAttachment(self, attach_id, channel):
        if channel not in self.saidAttachments:
            sayTimeout = self.registryValue('bugSnarferTimeout')
            self.saidAttachments[channel] = TimeoutQueue(sayTimeout)
        if attach_id in self.saidAttachments[channel]:
            return False
        self.saidAttachments[channel].enqueue(attach_id)
        return True

    def _bugError(self, bug, bug_url):
        error_type = bug.getAttribute('error')
        if error_type == 'NotFound':
            return 'Bug %s was not found.' % bug_url
        elif error_type == 'NotPermitted':
            return 'Bug %s is not accessible.' % bug_url
        return 'Bug %s could not be retrieved: %s' % (bug_url,  error_type)

    def _getBugs(self, url, ids, channel, show_url=True):
        """Returns an array of formatted strings describing the bug ids,
        using preferences appropriate to the passed-in channel."""

        bugs = self._getBugXml(url, ids)
        bug_strings = [];
        for bug in bugs:
            bug_id = bug.getElementsByTagName('bug_id')[0].childNodes[0].data
            if show_url:
                bug_url = '%s/show_bug.cgi?id=%s' % (url, urllib.quote(bug_id))
            else:
                bug_url = bug_id + ':'

            if bug.hasAttribute('error'):
                bug_strings.append(self._bugError(bug, bug_url))
            else:
                bug_data = []
                for field in self.registryValue('bugFormat', channel):
                    node_text = _getTagText(bug, field)
                    if node_text:
                        bug_data.append(node_text)
                bug_strings.append('Bug ' + bug_url + ' ' + ', '.join(bug_data))

        bug_strings = [self._formatLine(s, channel, 'bug') \
                       for s in bug_strings]
        return bug_strings

    def _getBugXml(self, url, ids):
        queryurl = url + '/show_bug.cgi?ctype=xml&excludefield=long_desc' \
                   + '&excludefield=attachmentdata'
        for id in ids:
            queryurl = queryurl + '&id=' + urllib.quote(str(id))

        self.log.debug('Getting bugs from %s' % queryurl)

        bugxml = utils.web.getUrl(queryurl)
        if not bugxml:
            raise callbacks.Error, 'Got empty bug content'

        return minidom.parseString(bugxml).getElementsByTagName('bug')

    def _getAttachments(self, url, attach_ids, channel):
        # The code for getting the title is copied from the Web plugin
        attach_url = '%s/attachment.cgi?id=%s&action=edit'
        attach_bugs = {}
        lines = []

        # Get the bug ID that each bug is on.
        for attach_id in attach_ids:
            my_url = attach_url % (url, attach_id)
            text = utils.web.getUrl(my_url, size=ATTACH_TITLE_SIZE)
            parser = Web.Title()
            try:
                parser.feed(text)
            except sgmllib.SGMLParseError:
                self.log.debug('Encountered a problem parsing %u.', my_url)
            title  = parser.title.strip()
            match  = re.search('Attachment.*bug (\d+)', title, re.I)
            if not match:
                err = 'Attachment %s was not found or is not accessible.' \
                       % attach_id
                lines.append(self._formatLine(err, channel, 'attachment'))
                continue
            bug_id = match.group(1)
            if bug_id not in attach_bugs:
                attach_bugs[bug_id] = []
            attach_bugs[bug_id].append(attach_id)

        # Get the attachment details
        for bug_id, attachments in attach_bugs.iteritems():
            self.log.debug('Getting attachments %r on bug %s' % \
                           (attachments, bug_id))
            attach_strings = self._getAttachmentsOnBug(url, attachments,
                                 bug_id, channel, do_error=True)
            lines.extend(attach_strings)
        return lines
 

    def _getAttachmentsOnBug(self, url, attach_ids, bug_id, channel, 
                             do_error=False):
        bug = self._getBugXml(url, [bug_id])[0]
        if bug.hasAttribute('error'):
            if do_error:
                return [self._bugError(bug, bug_id)]
            else:
                return []

        attachments = bug.getElementsByTagName('attachment')
        attach_strings = []
        # Sometimes we're passed ints, sometimes strings. We want to always
        # have a list of ints so that "in" works below.
        attach_ids = [int(id) for id in attach_ids]
        for attachment in attachments:
            attach_id = int(_getTagText(attachment, 'attachid'))
            if attach_id not in attach_ids: continue

            attach_url = '%s/attachment.cgi?id=%s&action=edit' % (url, attach_id)
            attach_data = []
            for field in self.registryValue('attachFormat', channel):
                node_text = _getTagText(attachment, field)
                if node_text:
                    if (field == 'type' 
                        and attachment.getAttribute('ispatch') == '1'):
                        node_text = 'patch'
                    attach_data.append(node_text)
            attach_strings.append('Attachment ' + attach_url + ' ' \
                                  + ', '.join(attach_data))
        attach_strings = [self._formatLine(s, channel, 'attachment') \
                          for s in attach_strings]
        return attach_strings
        
    def __call__(self, irc, msg):
        irc = callbacks.SimpleProxy(irc, msg)
        self.lastIrc = irc
        self.lastMsg = msg
        self.__parent.__call__(irc, msg)

    def _pollMbox(self):
        file_name = self.registryValue('mbox')
        if not file_name: return
        boxFile = open(file_name, 'r+b')
        _lock_file(boxFile)
        self.log.debug('Polling mbox %r' % boxFile)

        try:
            box = mailbox.PortableUnixMailbox(boxFile, _message_factory)
            bugmails = []
            for message in box:
                if message == '': continue
                self.log.debug('Parsing message %s' % message['Message-ID'])
                try:
                    bugmails.append(bugmail.Bugmail(message))
                except bugmail.NotBugmailException:
                    continue
                except: raise
            boxFile.truncate(0)
        finally:
            _unlock_file(boxFile)
            boxFile.close()

        self._handle_bugmails(self.lastIrc, bugmails)

    def _handle_bugmails(self, irc, bugmails):
        for bug in bugmails:
            self.log.debug('Handling bugmail for bug %d' % bug.bug_id)

            # Add the status into the resolution if they both changed.
            diffs = bug.diffs()
            resolution = bug.changed('Resolution')
            status     = bug.changed('Status')
            if status and resolution:
                status     = status[0]
                resolution = resolution[0]
                if resolution['added']:
                    status['added'] = status['added'] + ' ' \
                                     + resolution['added']
                if resolution['removed']:
                    status['removed'] = status['removed'] + ' ' \
                                        + resolution['removed']

            for channel in irc.state.channels.keys():
                self.log.debug('Handling bugmail in channel %s' % channel)
                # Determine whether or not we should mention this bug at
                # all in this channel.
                say_bug = False
                report  = self.registryValue('reportedChanges', channel)

                # If something was just removed from a particular field, we
                # want to still report that change in the proper channel.
                field_values = bug.fields()
                for field in field_values.keys():
                    array = [field_values[field]]
                    old_item = bug.changed(field)
                    if old_item:
                        array.append(old_item[0]['removed'])
                    field_values[field] = array

                for field, array in field_values.iteritems():
                    for value in array:
                        # Check the configuration for this product, component,
                        # etc.
                        try:
                            watch_list = self.registryValue('watchedItems.%s' \
                                                            % field, channel)
                            if value in watch_list:
                                say_bug = True
                                report = self.registryValue('watchedItems.%s.' \
                                         'reportedChanges' % field, channel)
                        except registry.NonExistentRegistryEntry:
                            continue
                        except: raise
                if self.registryValue('watchedItems.all', channel): 
                    say_bug = True
                if not say_bug: continue

                # Get the lines we should say about this bugmail
                lines = []
                say_attachments = []
                if 'newBug' in report and bug.new:
                    new_msg = self.registryValue('messages.newBug', channel)
                    lines.append(new_msg % bug.fields())
                if 'newAttach' in report and bug.attach_id:
                    attach_msg = self.registryValue('messages.newAttachment', channel)
                    lines.append(attach_msg % bug.fields())
                    if self._shouldSayAttachment(bug.attach_id, channel):
                        say_attachments.append(bug.attach_id)

                for diff in bug.diffs():
                    if (not ('All' in report or diff['what'] in report)):
                        continue

                    if ('attachment' in diff
                        # This is a bit of a hack.
                        and self._shouldSayAttachment(diff['attachment'],
                                                      channel)):
                        say_attachments.append(diff['attachment'])

                    # If we're watching both status and resolution, and both
                    # change, don't say Status--say resolution instead.
                    if (('Resolution' in report or 'All' in report)
                        and bug.changed('Resolution')
                        and bug.changed('Status')):
                        if diff['what'] == 'Status': continue
                        if diff['what'] == 'Resolution': 
                            diff = bug.changed('Status')[0]

                    bug_messages = self._diff_messages(channel, bug, diff)
                    lines.extend(bug_messages)

                # If we have anything to say in this channel about this
                # bug, then say it.
                if lines:
                    self.log.debug('Reporting %d change(s) to %s' \
                                   % (len(lines), channel))
                    lines = [self._formatLine(l, channel, 'change') \
                             for l in lines]
                    url = self.registryValue('bugzilla', channel)
                    if say_attachments:
                        attach_strings = self._getAttachmentsOnBug(url, \
                                             say_attachments, bug.bug_id, \
                                             channel)
                        lines.extend(attach_strings)
                    if self._shouldSayBug(bug.bug_id, channel):
                        lines.append(self._getBugs(url, [bug.bug_id], channel)[0])
                    if bug.dupe_of and self._shouldSayBug(bug.dupe_of, channel): 
                        lines.append(self._getBugs(url, [bug.dupe_of], channel)[0])
                    for line in lines:
                        irc.reply(line, prefixNick=False, to=channel, private=True)
 
    def _diff_messages(self, channel, bm, diff):
        lines = []

        attach_string = ''
        if diff.get('attachment', None):
            attach_string = ' for attachment ' + diff['attachment']

        bug_string = '%s on bug %d' % (attach_string, bm.bug_id)
        if 'flags' in diff:
            flags = diff['flags']
            for status, word in self.status_words.iteritems():
                for flag in flags[status]:
                    lines.append('%s %s %s%s.' % (bm.changer, word, 
                                                 flag['name'], bug_string))
            for flag in flags['?']:
                requestee = self.registryValue('messages.noRequestee', channel)
                if flag['requestee']: 
                    requestee = 'from ' + flag['requestee']
                lines.append('%s requested %s %s%s.' % (bm.changer, 
                             flag['name'], requestee, bug_string))
        else:
            what    = diff['what']
            removed = diff['removed']
            added   = diff['added']

            line = bm.changer
            if what in bugmail.MULTI_FIELDS:
                if added:             line += " added %s to" % added
                if added and removed: line += " and"
                if removed:           line += " removed %s from" % removed
                line += " the %s field%s." % (what, bug_string)
            elif (what in ['Resolution', 'Status'] and added.find('DUPLICATE') != -1):
                line += " marked bug %d as a duplicate of bug %d." % \
                        (bm.bug_id, bm.dupe_of)
            # We only added something.
            elif not removed:
                line += " set the %s field%s to %s." % \
                        (what, bug_string, added)
            # We only removed something
            elif not added:
                line += " cleared the %s '%s'%s." % \
                        (what, removed, bug_string)
            # We changed the value of a field from something to 
            # something else
            else:
                line += " changed the %s%s from %s to %s." % \
                        (what, bug_string, removed, added)

            lines.append(line)
        return lines

Class = Bugzilla

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
