# transaction.py - simple journaling scheme for mercurial
#
# This transaction scheme is intended to gracefully handle program
# errors and interruptions. More serious failures like system crashes
# can be recovered with an fsck-like tool. As the whole repository is
# effectively log-structured, this should amount to simply truncating
# anything that isn't referenced in the changelog.
#
# Copyright 2005, 2006 Matt Mackall <mpm@selenic.com>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from __future__ import absolute_import

import errno

from .i18n import _
from . import (
    error,
    pycompat,
    util,
)
from .utils import stringutil

version = 2

# These are the file generators that should only be executed after the
# finalizers are done, since they rely on the output of the finalizers (like
# the changelog having been written).
postfinalizegenerators = {b'bookmarks', b'dirstate'}

gengroupall = b'all'
gengroupprefinalize = b'prefinalize'
gengrouppostfinalize = b'postfinalize'


def active(func):
    def _active(self, *args, **kwds):
        if self._count == 0:
            raise error.Abort(
                _(
                    b'cannot use transaction when it is already committed/aborted'
                )
            )
        return func(self, *args, **kwds)

    return _active


def _playback(
    journal,
    report,
    opener,
    vfsmap,
    entries,
    backupentries,
    unlink=True,
    checkambigfiles=None,
):
    for f, o, _ignore in entries:
        if o or not unlink:
            checkambig = checkambigfiles and (f, b'') in checkambigfiles
            try:
                fp = opener(f, b'a', checkambig=checkambig)
                if fp.tell() < o:
                    raise error.Abort(
                        _(
                            b"attempted to truncate %s to %d bytes, but it was "
                            b"already %d bytes\n"
                        )
                        % (f, o, fp.tell())
                    )
                fp.truncate(o)
                fp.close()
            except IOError:
                report(_(b"failed to truncate %s\n") % f)
                raise
        else:
            try:
                opener.unlink(f)
            except (IOError, OSError) as inst:
                if inst.errno != errno.ENOENT:
                    raise

    backupfiles = []
    for l, f, b, c in backupentries:
        if l not in vfsmap and c:
            report(b"couldn't handle %s: unknown cache location %s\n" % (b, l))
        vfs = vfsmap[l]
        try:
            if f and b:
                filepath = vfs.join(f)
                backuppath = vfs.join(b)
                checkambig = checkambigfiles and (f, l) in checkambigfiles
                try:
                    util.copyfile(backuppath, filepath, checkambig=checkambig)
                    backupfiles.append(b)
                except IOError:
                    report(_(b"failed to recover %s\n") % f)
            else:
                target = f or b
                try:
                    vfs.unlink(target)
                except (IOError, OSError) as inst:
                    if inst.errno != errno.ENOENT:
                        raise
        except (IOError, OSError, error.Abort):
            if not c:
                raise

    backuppath = b"%s.backupfiles" % journal
    if opener.exists(backuppath):
        opener.unlink(backuppath)
    opener.unlink(journal)
    try:
        for f in backupfiles:
            if opener.exists(f):
                opener.unlink(f)
    except (IOError, OSError, error.Abort):
        # only pure backup file remains, it is sage to ignore any error
        pass


class transaction(util.transactional):
    def __init__(
        self,
        report,
        opener,
        vfsmap,
        journalname,
        undoname=None,
        after=None,
        createmode=None,
        validator=None,
        releasefn=None,
        checkambigfiles=None,
        name='<unnamed>',
    ):
        """Begin a new transaction

        Begins a new transaction that allows rolling back writes in the event of
        an exception.

        * `after`: called after the transaction has been committed
        * `createmode`: the mode of the journal file that will be created
        * `releasefn`: called after releasing (with transaction and result)

        `checkambigfiles` is a set of (path, vfs-location) tuples,
        which determine whether file stat ambiguity should be avoided
        for corresponded files.
        """
        self._count = 1
        self._usages = 1
        self._report = report
        # a vfs to the store content
        self._opener = opener
        # a map to access file in various {location -> vfs}
        vfsmap = vfsmap.copy()
        vfsmap[b''] = opener  # set default value
        self._vfsmap = vfsmap
        self._after = after
        self._entries = []
        self._map = {}
        self._journal = journalname
        self._undoname = undoname
        self._queue = []
        # A callback to validate transaction content before closing it.
        # should raise exception is anything is wrong.
        # target user is repository hooks.
        if validator is None:
            validator = lambda tr: None
        self._validator = validator
        # A callback to do something just after releasing transaction.
        if releasefn is None:
            releasefn = lambda tr, success: None
        self._releasefn = releasefn

        self._checkambigfiles = set()
        if checkambigfiles:
            self._checkambigfiles.update(checkambigfiles)

        self._names = [name]

        # A dict dedicated to precisely tracking the changes introduced in the
        # transaction.
        self.changes = {}

        # a dict of arguments to be passed to hooks
        self.hookargs = {}
        self._file = opener.open(self._journal, b"w")

        # a list of ('location', 'path', 'backuppath', cache) entries.
        # - if 'backuppath' is empty, no file existed at backup time
        # - if 'path' is empty, this is a temporary transaction file
        # - if 'location' is not empty, the path is outside main opener reach.
        #   use 'location' value as a key in a vfsmap to find the right 'vfs'
        # (cache is currently unused)
        self._backupentries = []
        self._backupmap = {}
        self._backupjournal = b"%s.backupfiles" % self._journal
        self._backupsfile = opener.open(self._backupjournal, b'w')
        self._backupsfile.write(b'%d\n' % version)

        if createmode is not None:
            opener.chmod(self._journal, createmode & 0o666)
            opener.chmod(self._backupjournal, createmode & 0o666)

        # hold file generations to be performed on commit
        self._filegenerators = {}
        # hold callback to write pending data for hooks
        self._pendingcallback = {}
        # True is any pending data have been written ever
        self._anypending = False
        # holds callback to call when writing the transaction
        self._finalizecallback = {}
        # hold callback for post transaction close
        self._postclosecallback = {}
        # holds callbacks to call during abort
        self._abortcallback = {}

    def __repr__(self):
        name = '/'.join(self._names)
        return '<transaction name=%s, count=%d, usages=%d>' % (
            name,
            self._count,
            self._usages,
        )

    def __del__(self):
        if self._journal:
            self._abort()

    @active
    def startgroup(self):
        """delay registration of file entry

        This is used by strip to delay vision of strip offset. The transaction
        sees either none or all of the strip actions to be done."""
        self._queue.append([])

    @active
    def endgroup(self):
        """apply delayed registration of file entry.

        This is used by strip to delay vision of strip offset. The transaction
        sees either none or all of the strip actions to be done."""
        q = self._queue.pop()
        for f, o, data in q:
            self._addentry(f, o, data)

    @active
    def add(self, file, offset, data=None):
        """record the state of an append-only file before update"""
        if file in self._map or file in self._backupmap:
            return
        if self._queue:
            self._queue[-1].append((file, offset, data))
            return

        self._addentry(file, offset, data)

    def _addentry(self, file, offset, data):
        """add a append-only entry to memory and on-disk state"""
        if file in self._map or file in self._backupmap:
            return
        self._entries.append((file, offset, data))
        self._map[file] = len(self._entries) - 1
        # add enough data to the journal to do the truncate
        self._file.write(b"%s\0%d\n" % (file, offset))
        self._file.flush()

    @active
    def addbackup(self, file, hardlink=True, location=b''):
        """Adds a backup of the file to the transaction

        Calling addbackup() creates a hardlink backup of the specified file
        that is used to recover the file in the event of the transaction
        aborting.

        * `file`: the file path, relative to .hg/store
        * `hardlink`: use a hardlink to quickly create the backup
        """
        if self._queue:
            msg = b'cannot use transaction.addbackup inside "group"'
            raise error.ProgrammingError(msg)

        if file in self._map or file in self._backupmap:
            return
        vfs = self._vfsmap[location]
        dirname, filename = vfs.split(file)
        backupfilename = b"%s.backup.%s" % (self._journal, filename)
        backupfile = vfs.reljoin(dirname, backupfilename)
        if vfs.exists(file):
            filepath = vfs.join(file)
            backuppath = vfs.join(backupfile)
            util.copyfile(filepath, backuppath, hardlink=hardlink)
        else:
            backupfile = b''

        self._addbackupentry((location, file, backupfile, False))

    def _addbackupentry(self, entry):
        """register a new backup entry and write it to disk"""
        self._backupentries.append(entry)
        self._backupmap[entry[1]] = len(self._backupentries) - 1
        self._backupsfile.write(b"%s\0%s\0%s\0%d\n" % entry)
        self._backupsfile.flush()

    @active
    def registertmp(self, tmpfile, location=b''):
        """register a temporary transaction file

        Such files will be deleted when the transaction exits (on both
        failure and success).
        """
        self._addbackupentry((location, b'', tmpfile, False))

    @active
    def addfilegenerator(
        self, genid, filenames, genfunc, order=0, location=b''
    ):
        """add a function to generates some files at transaction commit

        The `genfunc` argument is a function capable of generating proper
        content of each entry in the `filename` tuple.

        At transaction close time, `genfunc` will be called with one file
        object argument per entries in `filenames`.

        The transaction itself is responsible for the backup, creation and
        final write of such file.

        The `genid` argument is used to ensure the same set of file is only
        generated once. Call to `addfilegenerator` for a `genid` already
        present will overwrite the old entry.

        The `order` argument may be used to control the order in which multiple
        generator will be executed.

        The `location` arguments may be used to indicate the files are located
        outside of the the standard directory for transaction. It should match
        one of the key of the `transaction.vfsmap` dictionary.
        """
        # For now, we are unable to do proper backup and restore of custom vfs
        # but for bookmarks that are handled outside this mechanism.
        self._filegenerators[genid] = (order, filenames, genfunc, location)

    @active
    def removefilegenerator(self, genid):
        """reverse of addfilegenerator, remove a file generator function"""
        if genid in self._filegenerators:
            del self._filegenerators[genid]

    def _generatefiles(self, suffix=b'', group=gengroupall):
        # write files registered for generation
        any = False
        for id, entry in sorted(pycompat.iteritems(self._filegenerators)):
            any = True
            order, filenames, genfunc, location = entry

            # for generation at closing, check if it's before or after finalize
            postfinalize = group == gengrouppostfinalize
            if (
                group != gengroupall
                and (id in postfinalizegenerators) != postfinalize
            ):
                continue

            vfs = self._vfsmap[location]
            files = []
            try:
                for name in filenames:
                    name += suffix
                    if suffix:
                        self.registertmp(name, location=location)
                        checkambig = False
                    else:
                        self.addbackup(name, location=location)
                        checkambig = (name, location) in self._checkambigfiles
                    files.append(
                        vfs(name, b'w', atomictemp=True, checkambig=checkambig)
                    )
                genfunc(*files)
                for f in files:
                    f.close()
                # skip discard() loop since we're sure no open file remains
                del files[:]
            finally:
                for f in files:
                    f.discard()
        return any

    @active
    def find(self, file):
        if file in self._map:
            return self._entries[self._map[file]]
        if file in self._backupmap:
            return self._backupentries[self._backupmap[file]]
        return None

    @active
    def replace(self, file, offset, data=None):
        '''
        replace can only replace already committed entries
        that are not pending in the queue
        '''

        if file not in self._map:
            raise KeyError(file)
        index = self._map[file]
        self._entries[index] = (file, offset, data)
        self._file.write(b"%s\0%d\n" % (file, offset))
        self._file.flush()

    @active
    def nest(self, name='<unnamed>'):
        self._count += 1
        self._usages += 1
        self._names.append(name)
        return self

    def release(self):
        if self._count > 0:
            self._usages -= 1
        if self._names:
            self._names.pop()
        # if the transaction scopes are left without being closed, fail
        if self._count > 0 and self._usages == 0:
            self._abort()

    def running(self):
        return self._count > 0

    def addpending(self, category, callback):
        """add a callback to be called when the transaction is pending

        The transaction will be given as callback's first argument.

        Category is a unique identifier to allow overwriting an old callback
        with a newer callback.
        """
        self._pendingcallback[category] = callback

    @active
    def writepending(self):
        '''write pending file to temporary version

        This is used to allow hooks to view a transaction before commit'''
        categories = sorted(self._pendingcallback)
        for cat in categories:
            # remove callback since the data will have been flushed
            any = self._pendingcallback.pop(cat)(self)
            self._anypending = self._anypending or any
        self._anypending |= self._generatefiles(suffix=b'.pending')
        return self._anypending

    @active
    def hasfinalize(self, category):
        """check is a callback already exist for a category
        """
        return category in self._finalizecallback

    @active
    def addfinalize(self, category, callback):
        """add a callback to be called when the transaction is closed

        The transaction will be given as callback's first argument.

        Category is a unique identifier to allow overwriting old callbacks with
        newer callbacks.
        """
        self._finalizecallback[category] = callback

    @active
    def addpostclose(self, category, callback):
        """add or replace a callback to be called after the transaction closed

        The transaction will be given as callback's first argument.

        Category is a unique identifier to allow overwriting an old callback
        with a newer callback.
        """
        self._postclosecallback[category] = callback

    @active
    def getpostclose(self, category):
        """return a postclose callback added before, or None"""
        return self._postclosecallback.get(category, None)

    @active
    def addabort(self, category, callback):
        """add a callback to be called when the transaction is aborted.

        The transaction will be given as the first argument to the callback.

        Category is a unique identifier to allow overwriting an old callback
        with a newer callback.
        """
        self._abortcallback[category] = callback

    @active
    def close(self):
        '''commit the transaction'''
        if self._count == 1:
            self._validator(self)  # will raise exception if needed
            self._validator = None  # Help prevent cycles.
            self._generatefiles(group=gengroupprefinalize)
            while self._finalizecallback:
                callbacks = self._finalizecallback
                self._finalizecallback = {}
                categories = sorted(callbacks)
                for cat in categories:
                    callbacks[cat](self)
            # Prevent double usage and help clear cycles.
            self._finalizecallback = None
            self._generatefiles(group=gengrouppostfinalize)

        self._count -= 1
        if self._count != 0:
            return
        self._file.close()
        self._backupsfile.close()
        # cleanup temporary files
        for l, f, b, c in self._backupentries:
            if l not in self._vfsmap and c:
                self._report(
                    b"couldn't remove %s: unknown cache location %s\n" % (b, l)
                )
                continue
            vfs = self._vfsmap[l]
            if not f and b and vfs.exists(b):
                try:
                    vfs.unlink(b)
                except (IOError, OSError, error.Abort) as inst:
                    if not c:
                        raise
                    # Abort may be raise by read only opener
                    self._report(
                        b"couldn't remove %s: %s\n" % (vfs.join(b), inst)
                    )
        self._entries = []
        self._writeundo()
        if self._after:
            self._after()
            self._after = None  # Help prevent cycles.
        if self._opener.isfile(self._backupjournal):
            self._opener.unlink(self._backupjournal)
        if self._opener.isfile(self._journal):
            self._opener.unlink(self._journal)
        for l, _f, b, c in self._backupentries:
            if l not in self._vfsmap and c:
                self._report(
                    b"couldn't remove %s: unknown cache location"
                    b"%s\n" % (b, l)
                )
                continue
            vfs = self._vfsmap[l]
            if b and vfs.exists(b):
                try:
                    vfs.unlink(b)
                except (IOError, OSError, error.Abort) as inst:
                    if not c:
                        raise
                    # Abort may be raise by read only opener
                    self._report(
                        b"couldn't remove %s: %s\n" % (vfs.join(b), inst)
                    )
        self._backupentries = []
        self._journal = None

        self._releasefn(self, True)  # notify success of closing transaction
        self._releasefn = None  # Help prevent cycles.

        # run post close action
        categories = sorted(self._postclosecallback)
        for cat in categories:
            self._postclosecallback[cat](self)
        # Prevent double usage and help clear cycles.
        self._postclosecallback = None

    @active
    def abort(self):
        '''abort the transaction (generally called on error, or when the
        transaction is not explicitly committed before going out of
        scope)'''
        self._abort()

    def _writeundo(self):
        """write transaction data for possible future undo call"""
        if self._undoname is None:
            return
        undobackupfile = self._opener.open(
            b"%s.backupfiles" % self._undoname, b'w'
        )
        undobackupfile.write(b'%d\n' % version)
        for l, f, b, c in self._backupentries:
            if not f:  # temporary file
                continue
            if not b:
                u = b''
            else:
                if l not in self._vfsmap and c:
                    self._report(
                        b"couldn't remove %s: unknown cache location"
                        b"%s\n" % (b, l)
                    )
                    continue
                vfs = self._vfsmap[l]
                base, name = vfs.split(b)
                assert name.startswith(self._journal), name
                uname = name.replace(self._journal, self._undoname, 1)
                u = vfs.reljoin(base, uname)
                util.copyfile(vfs.join(b), vfs.join(u), hardlink=True)
            undobackupfile.write(b"%s\0%s\0%s\0%d\n" % (l, f, u, c))
        undobackupfile.close()

    def _abort(self):
        self._count = 0
        self._usages = 0
        self._file.close()
        self._backupsfile.close()

        try:
            if not self._entries and not self._backupentries:
                if self._backupjournal:
                    self._opener.unlink(self._backupjournal)
                if self._journal:
                    self._opener.unlink(self._journal)
                return

            self._report(_(b"transaction abort!\n"))

            try:
                for cat in sorted(self._abortcallback):
                    self._abortcallback[cat](self)
                # Prevent double usage and help clear cycles.
                self._abortcallback = None
                _playback(
                    self._journal,
                    self._report,
                    self._opener,
                    self._vfsmap,
                    self._entries,
                    self._backupentries,
                    False,
                    checkambigfiles=self._checkambigfiles,
                )
                self._report(_(b"rollback completed\n"))
            except BaseException as exc:
                self._report(_(b"rollback failed - please run hg recover\n"))
                self._report(
                    _(b"(failure reason: %s)\n") % stringutil.forcebytestr(exc)
                )
        finally:
            self._journal = None
            self._releasefn(self, False)  # notify failure of transaction
            self._releasefn = None  # Help prevent cycles.


def rollback(opener, vfsmap, file, report, checkambigfiles=None):
    """Rolls back the transaction contained in the given file

    Reads the entries in the specified file, and the corresponding
    '*.backupfiles' file, to recover from an incomplete transaction.

    * `file`: a file containing a list of entries, specifying where
    to truncate each file.  The file should contain a list of
    file\0offset pairs, delimited by newlines. The corresponding
    '*.backupfiles' file should contain a list of file\0backupfile
    pairs, delimited by \0.

    `checkambigfiles` is a set of (path, vfs-location) tuples,
    which determine whether file stat ambiguity should be avoided at
    restoring corresponded files.
    """
    entries = []
    backupentries = []

    fp = opener.open(file)
    lines = fp.readlines()
    fp.close()
    for l in lines:
        try:
            f, o = l.split(b'\0')
            entries.append((f, int(o), None))
        except ValueError:
            report(
                _(b"couldn't read journal entry %r!\n") % pycompat.bytestr(l)
            )

    backupjournal = b"%s.backupfiles" % file
    if opener.exists(backupjournal):
        fp = opener.open(backupjournal)
        lines = fp.readlines()
        if lines:
            ver = lines[0][:-1]
            if ver == (b'%d' % version):
                for line in lines[1:]:
                    if line:
                        # Shave off the trailing newline
                        line = line[:-1]
                        l, f, b, c = line.split(b'\0')
                        backupentries.append((l, f, b, bool(c)))
            else:
                report(
                    _(
                        b"journal was created by a different version of "
                        b"Mercurial\n"
                    )
                )

    _playback(
        file,
        report,
        opener,
        vfsmap,
        entries,
        backupentries,
        checkambigfiles=checkambigfiles,
    )
