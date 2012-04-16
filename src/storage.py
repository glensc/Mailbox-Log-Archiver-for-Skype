"""
Copyright (C) 2009 Y-NODE Software
Author: Alexander Tereshkin <atereshkin@y-node.com>

This program is free software; you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation; either version 2 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program; if not, write to the Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
"""

from __future__ import with_statement

import thread
import time
from datetime import datetime, timedelta
import smtplib
import string
import imaplib
import socket
import user
import os.path
import struct
from time import gmtime, strftime
from email.Utils import formatdate
import sys

import logging
log = logging.getLogger('mlas')



#How long a chat remains idle before it's archived to email.
#Smaller values lead to producing many emails for a single chat,
#larger values lead to longer archiving delay.
IDLE_TIMEOUT_SECONDS = 60


class EmailMessage(object):
    def __init__(self,
                 from_,
                 to,
                 subject,
                 timestamp,
                 msgid,
                 body,
                 start_dt,
                 end_dt,
                 ids):
        self.from_ = from_
        self.to = to
        self.subject = subject
        self.timestamp = timestamp
        self.msgid = msgid
        self.body = body
        self.start_dt = start_dt
        self.end_dt = end_dt
        self.ids = ids


class MailArchiver(object):
    """
    Base class for email-based log storages. Provides logic for grouping skype messages
    into email messages and delivery loop.
    """
    def __init__(self) :
        self._chats = {}
        self._lock = thread.allocate_lock()
        self._stopped = False
        self._email_queue = []
        self.persist_file = os.path.join(user.home, '.mlas_archived')
        from pprint import pprint
        if os.path.exists(self.persist_file):
            f = file(self.persist_file, 'rb')
            data = f.read()
            # /4 means 32bit data?
            num_items = len(data)/4
            self.delivered_msgs = set(struct.unpack('!%dL'%num_items, data))
            log.debug("loaded %d delivered messages" % (num_items))
            pprint(self.delivered_msgs)
            f.close()
        else:
            self.delivered_msgs = set()
        self.delivered_file=file(self.persist_file, 'wb')

    def _get_chat_data(self, chat):
        """
        Get chat instance based on timestamp
        """
        cd = self._chats.get(chat.Timestamp, [])
        self._chats[chat.Timestamp] = cd
        return cd

    def add(self, message):
        """
        Add a chat message to archive.
        """
#        log.debug("Checking messsage: %s" % message.Id)
        if message.Id in self.delivered_msgs:
            return
        with self._lock:
            log.debug("append: %s" % message.Id)
            self._get_chat_data(message.Chat).append(message)
            if self._stopped:
                log.debug("stopped, try to abord")
                raise KeyboardInterrupt

    def mark_added(self, message_id):
        self.delivered_msgs.add(message_id)
        self.delivered_file.write(struct.pack('!L', message_id))
        self.delivered_file.flush()

    def start(self):
        thread.start_new_thread(self._delivery_loop, ())

    def stop(self):
        with self._lock:
            self._stopped = True

    def _delivery_loop(self):
        log.debug("delivery loop started")
        while not self._stopped:
            with self._lock:
                now = datetime.now()
                for chat_stamp, chat in self._chats.iteritems():
                    if len(chat) > 0 and\
                            now - datetime.fromtimestamp(chat[-1].Timestamp) > timedelta(seconds=IDLE_TIMEOUT_SECONDS):
                        log.debug("deliver")
                        self.deliver_later(chat)
#                        last_msg = chat[-1]
                        chat[:]=[]
#                        chat.append(last_msg)
                    else:
                        log.debug("empty chatlog skipped")
            self.deliver_now()


    def deliver_later(self, chat):
        """
        Add a chat to mail delivery queue.
        """
        log.debug("Adding chat %s to delivery queue"%chat[0].Chat.FriendlyName)
        email_body = ''
        chat.sort(key=lambda msg : msg.Timestamp)
#        log.debug("chat sorted: %s" % ". ".join(map(lambda msg: msg.Id, chat)))
        for msg in chat:
            # check message type
#            log.debug("message: %r; type: %r; status: %r; edited: %r: body: %r" % (msg, msg.Type, msg.Status, msg.EditedBy, msg.Body))
            # msg.Users
            # TODO: are these msg.TYPE translated? should use enums.cmeSaid then
            invite_name = ""
            line_format = None
            if msg.Type == 'SAID' or msg.Type == 'EMOTED':
                if msg.EditedBy:
                    line_format = "%(id)d: [Edit by %(edit_by)s on %(edit_timestamp)s] [%(timestamp)s] %(emoted)s%(display_name)s%(sep)s %(body)s\n"
                else:
                    line_format = "%(id)d: [%(timestamp)s] %(emoted)s%(display_name)s%(sep)s %(body)s\n"

            elif msg.Type == 'SAWMEMBERS':
                users = ". ".join(map(lambda user: user.DisplayName or user.Handle, msg.Users))
                line_format = "%(id)d: [%(timestamp)s] type: ***" + msg.Type + " " + users + "\n"
                log.debug("saw members %r" % (msg.Users))

            elif msg.Type == 'LEFT':
                line_format = "%(id)d: [%(timestamp)s] * %(display_name)s left the chat.\n"
                log.debug("saw members %r" % (msg.Users))

            elif msg.Type == 'SETTOPIC':
                line_format = "%(id)d: [%(timestamp)s] * %(display_name)s has changed the chat topic to \"%(body)s\"\n";

            elif msg.Type == 'CREATEDCHATWITH' or msg.Type == 'ADDEDMEMBERS':
                line_format = "%(id)d: [%(timestamp)s] * %(display_name)s invited %(invite_name)s\n"
                invite_name = ". ".join(map(lambda user: user.DisplayName or user.Handle, msg.Users))

            else:
                line_format = "%(id)d: [%(timestamp)s] type: ***" + msg.Type + " from '" + str(msg.Sender) + "' [" + str(msg.FromHandle) + "] %(body)s\n"

            if line_format:
                email_body += line_format % {
                    'id': msg.Id,
                    'edit_by': msg.EditedBy,
                    'edit_timestamp': datetime.fromtimestamp(msg.EditedTimestamp),
                    'display_name': msg.FromDisplayName,
                    'invite_name': invite_name,
                    'timestamp': datetime.fromtimestamp(msg.Timestamp),
                    'body': msg.Body,
                    'emoted' : '* ' if msg.Type == 'EMOTED' else '',
                    'sep' : ':' if msg.Type != 'EMOTED' else '',
                }


        email_subject = '%d: "%s" (%s)' % (chat[0].Id, chat[0].Chat.FriendlyName,
                                                  datetime.fromtimestamp(chat[0].Chat.Timestamp))
        email = EmailMessage(from_=chat[0].Chat.DialogPartner, to=None,
                             subject=email_subject,
                             timestamp=time.localtime(chat[-1].Timestamp),
                             msgid=chat[-1].Chat.Name,
                             body=email_body,
                             start_dt=chat[0].Timestamp,
                             end_dt=chat[-1].Timestamp,
                             ids = [msg.Id for msg in chat])
        log.debug("created email message: %r" % email);

        self._email_queue.append(email)


    def deliver_now(self):
        """
        Implement backend-specific logic of saving message queue to server in this method.
        """
        raise NotImplementedError('MailArchiver shouldn\'t be used directly, but rather via one of its descendats')



class SMTPMailArchiver(MailArchiver):
    """
    Mail archiver using SMTP protocol.
    """

    def __init__(self,
                 smtp_host,
                 smtp_port,
                 smtp_use_tls,
                 smtp_user,
                 smtp_password,
                 email_address):
        """
        email_address is the address to use for "from" and "to" fields (mailbox owner address)
        """

        super(SMTPMailArchiver, self).__init__()
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_use_tls = smtp_use_tls
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.email_address = email_address
        self.smtp = smtplib.SMTP(local_hostname='localhost')
        self.start()

    def deliver_now(self):
        if len(self._email_queue) == 0:
            return
        self.smtp.connect(self.smtp_host, self.smtp_port)
        if self.smtp_use_tls:
            self.smtp.ehlo()
            self.smtp.starttls()
            self.smtp.ehlo()
        self.smtp.login(self.smtp_user, self.smtp_password)
        for email in self._email_queue:
            body = string.join((
                    "From: %s" % self.email_address,
                    "To: %s" % self.email_address,
                    "Subject: %s" % email[0],
                    "Content-Type: text/plain; charset=UTF-8",
                    "",
                    email[1]), "\r\n")
            self.smtp.sendmail(self.email_address, [self.email_address], body.encode('utf-8'))
        self.smtp.quit()
        self._email_queue = []


IMAP_FOLDER_NAME = 'Skype chats'
CHECK_CONNECTION_TIMEOUT = 10 # Socket timout to use when checking IMAP connection.

class IMAPMailArchiver(MailArchiver):

    def __init__(self,
                 imap_host,
                 imap_port,
                 imap_use_tls,
                 imap_user,
                 imap_password):
        super(IMAPMailArchiver, self).__init__()

        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_use_tls = imap_use_tls
        self.imap_user = imap_user
        self.imap_password = imap_password
        self.imap = None
        self.connect()
        self.start()


    def connect(self, retry=True):
        self.imap = None
        while not self.imap:
            log.debug("Connecting to IMAP server.")
            try:
                if self.imap_use_tls:
                    self.imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
                else:
                    self.imap = imaplib.IMAP4(self.imap_host, self.imap_port)
            except:
                self.imap = None
                if not retry:
                    raise
                time.sleep(10)
        log.debug("Connected to IMAP server. Authenticating.")
        self.imap.login(self.imap_user, self.imap_password)
        log.debug("Successfully authenticated to mail server.")
        if not self.imap.list(IMAP_FOLDER_NAME)[1][0]:
            log.debug("Creating log folder on the server.")
            self.imap.create(IMAP_FOLDER_NAME)

    def check_connection(self):
        """
        Check if the connection to mail server is still active.
        """
        sock = self.imap.socket()
        old_timeout = sock.gettimeout()
        sock.settimeout(CHECK_CONNECTION_TIMEOUT)
        try:
            noop_result = self.imap.noop()
            log.debug("imap noop: %r" % (noop_result,))
            return noop_result[0] == 'OK'
        except socket.error:
            return False
        finally:
            sock.settimeout(old_timeout)

    def deliver_now(self):
        log.debug("deliver_now")
        if len(self._email_queue) == 0:
            log.debug("Nothing to deliver")
            return

        if not self.check_connection():
            self.connect()

        # TODO: encode mail headers (From, Subject)
        for email in self._email_queue:
            rfcdate = formatdate(time.mktime(email.timestamp), localtime=True)
            log.debug("Add email: date: %r, msgid: %r", rfcdate, email.msgid)

            body = string.join((
                    "From: %s" % email.from_,
                    "Content-Type: text/plain; charset=UTF-8",
                    "Message-Id: <%s.%s>" % (email.msgid, email.end_dt),
                    "In-Reply-To: <%s.%s>" % (email.msgid, email.start_dt),
                    "Date: %s" % rfcdate,
                    "Subject: %s" % email.subject,
                    "",
                    email.body), "\r\n")
            try:
                self.imap.append(IMAP_FOLDER_NAME, '(\\Seen)', email.timestamp , body.encode('utf-8'))
            except:
                log.debug("Failed do send: %r" % body.encode('utf-8'))
                raise

            log.debug("appended");
            for id in email.ids:
                self.mark_added(id)
        self._email_queue = []
