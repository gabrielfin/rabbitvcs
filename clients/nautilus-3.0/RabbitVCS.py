#
# This is an extension to the Nautilus file manager to allow better
# integration with the Subversion source control system.
#
# Copyright (C) 2006-2008 by Jason Field <jason@jasonfield.com>
# Copyright (C) 2007-2008 by Bruce van der Kooij <brucevdkooij@gmail.com>
# Copyright (C) 2008-2008 by Adam Plumb <adamplumb@gmail.com>
#
# RabbitVCS is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# RabbitVCS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with RabbitVCS;  If not, see <http://www.gnu.org/licenses/>.
#

"""

Our module for everything related to the Nautilus extension.

"""
from __future__ import with_statement

def log_all_exceptions(type, value, tb):
    import sys, traceback
    from rabbitvcs.util.log import Log
    log = Log("rabbitvcs.util.extensions.Nautilus.RabbitVCS")
    log.exception_info("Error caught by master exception hook!",
                       (type, value, tb))

    text = ''.join(traceback.format_exception(type, value,
                                              tb, limit=None))

    try:
        import rabbitvcs.ui.dialog
        rabbitvcs.ui.dialog.ErrorNotification(text)
    except Exception, ex:
        log.exception("Additional exception when attempting"
                      " to display error dialog.")
        log.exception(ex)
        raise

    sys.__excepthook__(type, value, tb)

# import sys
# sys.excepthook = log_all_exceptions

import copy

import os
os.environ["NAUTILUS_PYTHON_REQUIRE_GTK3"] = "1"

import os.path
from os.path import isdir, isfile, realpath, basename
import datetime

from gi.repository import Nautilus, GObject

import pysvn

from rabbitvcs.vcs import VCS
import rabbitvcs.vcs.status

from rabbitvcs.util.helper import launch_ui_window, launch_diff_tool
from rabbitvcs.util.helper import get_file_extension, get_common_directory
from rabbitvcs.util.helper import pretty_timedelta

from rabbitvcs.util.decorators import timeit, disable

from rabbitvcs.util.contextmenu import MenuBuilder, MainContextMenu, SEPARATOR, ContextMenuConditions

import rabbitvcs.ui
import rabbitvcs.ui.property_page

from rabbitvcs.util.log import Log, reload_log_settings
log = Log("rabbitvcs.util.extensions.Nautilus.RabbitVCS")

from rabbitvcs import gettext
_ = gettext.gettext

from rabbitvcs import version as EXT_VERSION

from rabbitvcs.util.settings import SettingsManager
settings = SettingsManager()

import rabbitvcs.services.service
from rabbitvcs.services.checkerservice import StatusCheckerStub as StatusChecker

class RabbitVCS(Nautilus.InfoProvider, Nautilus.MenuProvider,
                 Nautilus.ColumnProvider, Nautilus.PropertyPageProvider, GObject.GObject):
    """
    This is the main class that implements all of our awesome features.

    """

    #: This is our lookup table for C{NautilusVFSFile}s which we need for attaching
    #: emblems. This is mostly a workaround for not being able to turn a path/uri
    #: into a C{NautilusVFSFile}. It looks like:::
    #:
    #:     nautilusVFSFile_table = {
    #:        "/foo/bar/baz": <NautilusVFSFile>
    #:
    #:     }
    #:
    #: Keeping track of C{NautilusVFSFile}s is a little bit complicated because
    #: when an item is moved (renamed) C{update_file_info} doesn't get called. So
    #: we also add C{NautilusVFSFile}s to this table from C{get_file_items} etc.
    # FIXME: this may be the source of the memory hogging seen in the extension
    # script itself.
    nautilusVFSFile_table = {}

    #: This is in case we want to permanently enable invalidation of the status
    #: checker info.
    always_invalidate = True

    #: When we get the statuses from the callback, put them here for further
    #: use. This is of the form: [("path/to", {...status dict...}), ...]
    statuses_from_callback = []

    def get_local_path(self, path):
        return path.replace("file://", "")

    def __init__(self):
        # Create a global client we can use to do VCS related stuff
        self.vcs_client = VCS()

        self.status_checker = StatusChecker()
        
        self.status_checker.assert_version(EXT_VERSION)
        
        self.items_cache = {}
        
    def get_columns(self):
        """
        Return all the columns we support.

        """

        return (
            Nautilus.Column(
                name="RabbitVCS::status_column",
                attribute="status",
                label=_("RVCS Status"),
                description=""
            ),
            Nautilus.Column(
                name="RabbitVCS::revision_column",
                attribute="revision",
                label=_("RVCS Revision"),
                description=""
            ),
            Nautilus.Column(
                name="RabbitVCS::author_column",
                attribute="author",
                label=_("RVCS Author"),
                description=""
            ),
            Nautilus.Column(
                name="RabbitVCS::age_column",
                attribute="age",
                label=_("RVCS Age"),
                description=""
            )
        )

    def update_file_info(self, item):
        """

        C{update_file_info} is called only when:

          - When you enter a directory (once for each item but only when the
            item was modified since the last time it was listed)
          - When you refresh (once for each item visible)
          - When an item viewable from the current window is created or modified

        This is insufficient for our purpose because:

          - You're not notified about items you don't see (which is needed to
            keep the emblem for the directories above the item up-to-date)

        @type   item: NautilusVFSFile
        @param  item:

        """
        enable_emblems = bool(int(settings.get("general", "enable_emblems")))
        enable_attrs = bool(int(settings.get("general", "enable_attributes")))
        
        if not (enable_emblems or enable_attrs): return Nautilus.OperationResult.COMPLETE
                
        if not self.valid_uri(item.get_uri()): return Nautilus.OperationResult.FAILED

        path = unicode(self.get_local_path(item.get_uri()), "utf-8")

        # log.debug("update_file_info() called for %s" % path)

        invalidate = False
        if path in self.nautilusVFSFile_table:
            invalidate = True

        # Always replace the item in the table with the one we receive, because
        # for example if an item is deleted and recreated the NautilusVFSFile
        # we had before will be invalid (think pointers and such).
        self.nautilusVFSFile_table[path] = item

        # This check should be pretty obvious :-)
        # TODO: how come the statuses for a few directories are incorrect
        # when we remove this line (detected as working copies, even though
        # they are not)? That shouldn't happen.
        is_in_a_or_a_working_copy = self.vcs_client.is_in_a_or_a_working_copy(path)
        if not is_in_a_or_a_working_copy: return Nautilus.OperationResult.COMPLETE

        # Do our magic...

        # I have added extra logic in cb_status, using a list
        # (paths_from_callback) that should allow us to work around this for
        # now. But it'd be good to have an actual status monitor.

        found = False
        status = None
        # Could replace with (st for st in self.... if st.path ...).next()
        # Need to catch exception
        for idx in xrange(len(self.statuses_from_callback)):
            found = (self.statuses_from_callback[idx].path) == path
            if found: break

        if found: # We're here because we were triggered by a callback
            status = self.statuses_from_callback[idx]
            del self.statuses_from_callback[idx]

        # Don't bother the checker if we already have the info from a callback
        if not found:
            status = \
                self.status_checker.check_status(path,
                                                 recurse=True,
                                                 summary=True,
                                                 callback=self.cb_status,
                                                 invalidate=invalidate)

        # FIXME: when did this get disabled?
        if enable_attrs: self.update_columns(item, path, status)
        if enable_emblems: self.update_status(item, path, status)
        
        return Nautilus.OperationResult.COMPLETE

    def update_columns(self, item, path, status):
        """
        Update the columns (attributes) for a given Nautilus item,
        filling them in with information from the version control
        server.

        """

        revision = ""
        if status.revision:
            revision = str(status.revision)

        age = ""
        if status.date:
            age = pretty_timedelta(
                datetime.datetime.fromtimestamp(status.date),
                datetime.datetime.now()
            )

        author = ""
        if status.author:
            author = str(status.author)

        values = {
            "status": status.simple_content_status(),
            "revision": revision,
            "author": author,
            "age": age
        }

        for key, value in values.items():
            item.add_string_attribute(key, value)

    def update_status(self, item, path, status):
        if status.summary in rabbitvcs.ui.STATUS_EMBLEMS:
            item.add_emblem(rabbitvcs.ui.STATUS_EMBLEMS[status.summary])

    #~ @disable
    # @timeit
    # FIXME: this is a bottleneck. See generate_statuses() in
    # MainContextMenuConditions.
    def get_file_items_full(self, provider, window, items):
        """
        Menu activated with items selected. Nautilus also calls this function
        when rendering submenus, even though this is not needed since the entire
        menu has already been returned.

        Note that calling C{nautilusVFSFile.invalidate_extension_info()} will
        also cause get_file_items to be called.

        @type   window: NautilusNavigationWindow
        @param  window:

        @type   items:  list of NautilusVFSFile
        @param  items:

        @rtype:         list of MenuItems
        @return:        The context menu entries to add to the menu.

        """

        paths = []
        for item in items:
            if self.valid_uri(item.get_uri()):
                path = unicode(self.get_local_path(item.get_uri()), "utf-8")
                paths.append(path)
                self.nautilusVFSFile_table[path] = item

        if len(paths) == 0: return []
        
        # log.debug("get_file_items_full() called")

        paths_str = "-".join(paths)
        
        conditions_dict = None
        if paths_str in self.items_cache:
            conditions_dict = self.items_cache[paths_str]
            if conditions_dict and conditions_dict != "in-progress":
                conditions = NautilusMenuConditions(conditions_dict)
                menu = NautilusMainContextMenu(self, window.get_data("base_dir"), paths, conditions).get_menu()
                return menu
        
        if conditions_dict != "in-progress":
            self.status_checker.generate_menu_conditions_async(provider, window.get_data("base_dir"), paths, self.update_file_items)        
            self.items_cache[path] = "in-progress"
            
        return ()

    def get_file_items(self, window, items):
        paths = []
        for item in items:
            if self.valid_uri(item.get_uri()):
                path = unicode(self.get_local_path(item.get_uri()), "utf-8")
                paths.append(path)
                self.nautilusVFSFile_table[path] = item

        if len(paths) == 0: return []
        
        # log.debug("get_file_items() called")
        
        return NautilusMainContextMenu(self, window.get_data("base_dir"), paths).get_menu()

    def update_file_items(self, provider, base_dir, paths, conditions_dict):
        paths_str = "-".join(paths)
        self.items_cache[paths_str] =  conditions_dict
        Nautilus.MenuProvider.emit_items_updated_signal(provider)

    #~ @disable
    # This is useful for profiling. Rename it to "get_background_items" and then
    # rename the real function "get_background_items_real". 
    def get_background_items_profile(self, window, item):
        import cProfile
        import rabbitvcs.util.helper
        
        path = unicode(gnomevfs.get_local_path_from_uri(item.get_uri()),
                       "utf-8").replace("/", ":")
        
        profile_data_file = os.path.join(
                               rabbitvcs.util.helper.get_home_folder(),
                               "checkerservice_%s.stats" % path)
        
        prof = cProfile.Profile()
        retval = prof.runcall(self.get_background_items_real, window, item)
        prof.dump_stats(profile_data_file)
        log.debug("Dumped: %s" % profile_data_file)
        return retval
       
    def get_background_items_full(self, provider, window, item):
        """
        Menu activated on entering a directory. Builds context menu for File
        menu and for window background.

        @type   window: NautilusNavigationWindow
        @param  window:

        @type   item:   NautilusVFSFile
        @param  item:

        @rtype:         list of MenuItems
        @return:        The context menu entries to add to the menu.

        """

        if not self.valid_uri(item.get_uri()): return
        path = unicode(self.get_local_path(item.get_uri()), "utf-8")
        self.nautilusVFSFile_table[path] = item

        # log.debug("get_background_items_full() called")

        conditions_dict = None
        if path in self.items_cache:
            conditions_dict = self.items_cache[path]
            if conditions_dict and conditions_dict != "in-progress":
                conditions = NautilusMenuConditions(conditions_dict)
                menu = NautilusMainContextMenu(self, path, [path], conditions).get_menu()                
                return menu

        window.set_data("base_dir", path)

        if conditions_dict != "in-progress":
            self.status_checker.generate_menu_conditions_async(provider, path, [path], self.update_background_items)
            self.items_cache[path] = "in-progress"
                    
        return ()

    def get_background_items(self, window, item):
        if not self.valid_uri(item.get_uri()): return
        path = unicode(self.get_local_path(item.get_uri()), "utf-8")
        self.nautilusVFSFile_table[path] = item

        # log.debug("get_background_items() called")
        
        window.set_data("base_dir", path)
        
        return NautilusMainContextMenu(self, path, [path]).get_menu()

    def update_background_items(self, provider, base_dir, paths, conditions_dict):
        paths_str = "-".join(paths)
        conditions = NautilusMenuConditions(conditions_dict)
        self.items_cache[paths_str] =  conditions_dict
        Nautilus.MenuProvider.emit_items_updated_signal(provider)

    #
    # Helper functions
    #

    def valid_uri(self, uri):
        """
        Check whether or not it's a good idea to have RabbitVCS do
        its magic for this URI. Some examples of URI schemes:

        x-nautilus-desktop:/// # e.g. mounted devices on the desktop

        """

        if not uri.startswith("file://"): return False

        return True

    #
    # Some methods to help with keeping emblems up-to-date
    #

    def rescan_after_process_exit(self, proc, paths):

        def do_check():
            # We'll check the paths first (these were the paths that
            # were originally passed along to the context menu).
            #
            # This is needed among other things for:
            #
            #   - When a directory is normal and you add files inside it
            #
            for path in paths:
                # We're not interested in the result now, just the callback
                self.status_checker.check_status(path,
                                                 recurse=True,
                                                 invalidate=True,
                                                 callback=self.cb_status,
                                                 summary=True)

        self.execute_after_process_exit(proc, do_check)

    def execute_after_process_exit(self, proc, func=None):

        def is_process_still_alive():
            log.debug("is_process_still_alive() for pid: %i" % proc.pid)
            # First we need to see if the commit process is still running

            retval = proc.poll()

            log.debug("%s" % retval)

            still_going = (retval is None)

            if not still_going and callable(func):
                func()

            return still_going

        # Add our callback function on a 1 second timeout
        GObject.timeout_add_seconds(1, is_process_still_alive)

    #
    # Some other methods
    #

    def reload_settings(self, proc):
        """
        Used to re-load settings after the settings dialog has been closed.

        FIXME: This probably doesn't belong here, ideally the settings manager
        does this itself and make sure everything is reloaded properly
        after the settings dialogs saves.
        """

        def do_reload_settings():
            globals()["settings"] = SettingsManager()
            globals()["log"] = reload_log_settings()("rabbitvcs.util.extensions.nautilus")
            log.debug("Re-scanning settings")

        self.execute_after_process_exit(proc, do_reload_settings)


    #
    # Callbacks
    #

    def cb_status(self, status):
        """
        This is the callback that C{StatusMonitor} calls.

        @type   path:   string
        @param  path:   The path of the item something interesting happened to.

        @type   statuses: list of status objects
        @param  statuses: The statuses
        """
        if status.path in self.nautilusVFSFile_table:
            item = self.nautilusVFSFile_table[status.path]
            # We need to invalidate the extension info for only one reason:
            #
            # - Invalidating the extension info will cause Nautilus to remove all
            #   temporary emblems we applied so we don't have overlay problems
            #   (with ourselves, we'd still have some with other extensions).
            #
            # After invalidating C{update_file_info} applies the correct emblem.
            # Since invalidation triggers an "update_file_info" call, we can
            # tell it NOT to invalidate the status checker path.
            self.statuses_from_callback.append(status)
            # NOTE! There is a call to "update_file_info" WITHIN the call to
            # invalidate_extension_info() - beware recursion!
            item.invalidate_extension_info()
            if status.path in self.items_cache:
                del self.items_cache[status.path]
        else:
            log.debug("Path [%s] not found in file table" % status.path)

    def get_property_pages(self, items):
        paths = []

        for item in items:
            if self.valid_uri(item.get_uri()):
                path = unicode(self.get_local_path(item.get_uri()), "utf-8")
                
                if self.vcs_client.is_in_a_or_a_working_copy(path):
                    paths.append(path)
                    self.nautilusVFSFile_table[path] = item

        if len(paths) == 0: return []

        label = rabbitvcs.ui.property_page.PropertyPageLabel(claim_domain=False).get_widget()
        page = rabbitvcs.ui.property_page.PropertyPage(paths, claim_domain=False).get_widget()

        ppage = Nautilus.PropertyPage('RabbitVCS::PropertyPage',
            label,
            page)

        return [ppage]

from rabbitvcs.util.contextmenuitems import *

class NautilusContextMenu(MenuBuilder):
    """
    Provides a standard Nautilus context menu (ie. a list of
    "Nautilus.MenuItem"s).
    """

    signal = "activate"

    def make_menu_item(self, item, id_magic):
        return item.make_nautilus_menu_item(id_magic)

    def attach_submenu(self, menu_node, submenu_list):
        submenu = Nautilus.Menu()
        menu_node.set_submenu(submenu)
        [submenu.append_item(item) for item in submenu_list]

    def top_level_menu(self, items):
        return items

class NautilusMenuConditions(ContextMenuConditions):
    def __init__(self, path_dict):
        self.path_dict = path_dict

class NautilusMainContextMenu(MainContextMenu):
    def get_menu(self):
        return NautilusContextMenu(self.structure, self.conditions, self.callbacks).menu