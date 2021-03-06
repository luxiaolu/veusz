# -*- coding: utf-8 -*-
#    Copyright (C) 2003 Jeremy S. Sanders
#    Email: Jeremy Sanders <jeremy@jeremysanders.net>
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
##############################################################################

"""Implements the main window of the application."""

from __future__ import division
import os.path
import sys
import traceback
import glob

from ..compat import citems, ckeys, cstr, cexec, cstrerror
from .. import qtall as qt4

from .. import document
from .. import utils
from ..utils import vzdbus
from .. import setting
from .. import plugins

from . import consolewindow
from . import plotwindow
from . import treeeditwindow
from .datanavigator import DataNavigatorWindow

from ..dialogs.aboutdialog import AboutDialog
from ..dialogs.reloaddata import ReloadData
from ..dialogs.datacreate import DataCreateDialog
from ..dialogs.datacreate2d import DataCreate2DDialog
from ..dialogs.preferences import PreferencesDialog
from ..dialogs.errorloading import ErrorLoadingDialog
from ..dialogs.capturedialog import CaptureDialog
from ..dialogs.stylesheet import StylesheetDialog
from ..dialogs.custom import CustomDialog
from ..dialogs.safetyimport import SafetyImportDialog
from ..dialogs.histodata import HistoDataDialog
from ..dialogs.plugin import handlePlugin
from ..dialogs import importdialog
from ..dialogs import dataeditdialog

def _(text, disambiguation=None, context='MainWindow'):
    """Translate text."""
    return qt4.QCoreApplication.translate(context, text, disambiguation)

# shortcut to this
setdb = setting.settingdb

class DBusWinInterface(vzdbus.Object):
    """Simple DBus interface to window for triggering actions."""

    interface = 'org.veusz.actions'

    def __init__(self, actions, index):
        prefix = '/Windows/%i/Actions' % index
        vzdbus.Object.__init__(self, vzdbus.sessionbus, prefix)
        self.actions = actions

    @vzdbus.method(dbus_interface=interface, out_signature='as')
    def GetActions(self):
        """Get list of actions which can be activated."""
        return sorted(ckeys(self.actions))

    @vzdbus.method(dbus_interface=interface, in_signature='s')
    def TriggerAction(self, action):
        """Activate action given."""
        self.actions[action].trigger()

class MainWindow(qt4.QMainWindow):
    """ The main window class for the application."""

    windows = []
    @classmethod
    def CreateWindow(cls, filename=None):
        """Window factory function.

        If filename is given then that file is loaded into the window.
        Returns window created
        """

        # create the window, and optionally load a saved file
        win = cls()
        win.show()
        if filename:
            # load document
            win.openFileInWindow(filename)
        else:
            win.setupDefaultDoc()

        # try to select first graph of first page
        win.treeedit.doInitialWidgetSelect()

        cls.windows.append(win)

        # check if tutorial wanted
        if not setting.settingdb['ask_tutorial']:
            win.askTutorial()
        # don't ask again
        setting.settingdb['ask_tutorial'] = True

        return win

    def __init__(self, *args):
        qt4.QMainWindow.__init__(self, *args)
        self.setAcceptDrops(True)

        # icon and different size variations
        self.setWindowIcon( utils.getIcon('veusz') )

        # master documenent
        self.document = document.Document()

        # filename for document and update titlebar
        self.filename = ''
        self.updateTitlebar()

        # keep a list of references to dialogs
        self.dialogs = []

        # construct menus and toolbars
        self._defineMenus()

        # make plot window
        self.plot = plotwindow.PlotWindow(self.document, self,
                                          menu = self.menus['view'])
        self.setCentralWidget(self.plot)
        self.plot.showToolbar()

        # likewise with the tree-editing window
        self.treeedit = treeeditwindow.TreeEditDock(self.document, self)
        self.addDockWidget(qt4.Qt.LeftDockWidgetArea, self.treeedit)
        self.propdock = treeeditwindow.PropertiesDock(self.document,
                                                      self.treeedit, self)
        self.addDockWidget(qt4.Qt.LeftDockWidgetArea, self.propdock)
        self.formatdock = treeeditwindow.FormatDock(self.document,
                                                    self.treeedit, self)
        self.addDockWidget(qt4.Qt.LeftDockWidgetArea, self.formatdock)
        self.datadock = DataNavigatorWindow(self.document, self, self)
        self.addDockWidget(qt4.Qt.RightDockWidgetArea, self.datadock)

        # make the console window a dock
        self.console = consolewindow.ConsoleWindow(self.document,
                                                   self)
        self.console.hide()
        self.interpreter = self.console.interpreter
        self.addDockWidget(qt4.Qt.BottomDockWidgetArea, self.console)

        # assemble the statusbar
        statusbar = self.statusbar = qt4.QStatusBar(self)
        self.setStatusBar(statusbar)
        self.updateStatusbar(_('Ready'))

        # a label for the picker readout
        self.pickerlabel = qt4.QLabel(statusbar)
        self._setPickerFont(self.pickerlabel)
        statusbar.addPermanentWidget(self.pickerlabel)
        self.pickerlabel.hide()

        # plot queue - how many plots are currently being drawn
        self.plotqueuecount = 0
        self.connect( self.plot, qt4.SIGNAL("queuechange"),
                      self.plotQueueChanged )
        self.plotqueuelabel = qt4.QLabel()
        self.plotqueuelabel.setToolTip(_("Number of rendering jobs remaining"))
        statusbar.addWidget(self.plotqueuelabel)
        self.plotqueuelabel.show()

        # a label for the cursor position readout
        self.axisvalueslabel = qt4.QLabel(statusbar)
        statusbar.addPermanentWidget(self.axisvalueslabel)
        self.axisvalueslabel.show()
        self.slotUpdateAxisValues(None)

        # a label for the page number readout
        self.pagelabel = qt4.QLabel(statusbar)
        statusbar.addPermanentWidget(self.pagelabel)
        self.pagelabel.show()

        # working directory - use previous one
        self.dirname = setdb.get('dirname', qt4.QDir.homePath())
        self.dirname_export = setdb.get('dirname_export', self.dirname)
        if setdb['dirname_usecwd']:
            self.dirname = self.dirname_export = os.getcwd()

        # connect plot signals to main window
        self.connect( self.plot, qt4.SIGNAL("sigUpdatePage"),
                      self.slotUpdatePage )
        self.connect( self.plot, qt4.SIGNAL("sigAxisValuesFromMouse"),
                      self.slotUpdateAxisValues )
        self.connect( self.plot, qt4.SIGNAL("sigPickerEnabled"),
                      self.slotPickerEnabled )
        self.connect( self.plot, qt4.SIGNAL("sigPointPicked"),
                      self.slotUpdatePickerLabel )

        # disable save if already saved
        self.connect( self.document, qt4.SIGNAL("sigModified"),
                      self.slotModifiedDoc )
        # if the treeeditwindow changes the page, change the plot window
        self.connect( self.treeedit, qt4.SIGNAL("sigPageChanged"),
                      self.plot.setPageNumber )

        # if a widget in the plot window is clicked by the user
        self.connect( self.plot, qt4.SIGNAL("sigWidgetClicked"),
                      self.treeedit.selectWidget )
        self.connect( self.treeedit, qt4.SIGNAL("widgetsSelected"),
                      self.plot.selectedWidgets )

        # enable/disable undo/redo
        self.connect(self.menus['edit'], qt4.SIGNAL('aboutToShow()'),
                     self.slotAboutToShowEdit)

        #Get the list of recently opened files
        self.populateRecentFiles()
        self.setupWindowGeometry()
        self.defineViewWindowMenu()

        # if document requests it, ask whether an allowed import
        self.connect(self.document, qt4.SIGNAL('check_allowed_imports'),
                     self.slotAllowedImportsDoc)

        # add on dbus interface
        self.dbusdocinterface = document.DBusInterface(self.document)
        self.dbuswininterface = DBusWinInterface(
            self.vzactions, self.dbusdocinterface.index)

    def updateStatusbar(self, text):
        '''Display text for a set period.'''
        self.statusBar().showMessage(text, 2000)

    def dragEnterEvent(self, event):
        """Check whether event is valid to be dropped."""
        if (event.provides("text/uri-list") and
            self._getVeuszDropFiles(event)):
            event.acceptProposedAction()

    def dropEvent(self, event):
        """Respond to a drop event on the current window"""
        if event.provides("text/uri-list"):
            files = self._getVeuszDropFiles(event)
            if files:
                if self.document.isBlank():
                    self.openFileInWindow(files[0])
                else:
                    self.CreateWindow(files[0])
                for filename in files[1:]:
                    self.CreateWindow(filename)

    def _getVeuszDropFiles(self, event):
        """Return a list of veusz files from a drag/drop event containing a
        text/uri-list"""

        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        else:
            # get list of vsz files dropped
            urls = [u.path() for u in mime.urls()]
            urls = [u for u in urls if os.path.splitext(u)[1] == '.vsz']
            return urls

    def setupDefaultDoc(self):
        """Setup default document."""

        # add page and default graph
        self.document.makeDefaultDoc()

        # load defaults if set
        self.loadDefaultStylesheet()
        self.loadDefaultCustomDefinitions()

    def loadDefaultStylesheet(self):
        """Loads the default stylesheet for the new document."""
        filename = setdb['stylesheet_default']
        if filename:
            try:
                self.document.applyOperation(
                    document.OperationLoadStyleSheet(filename) )
            except EnvironmentError as e:
                qt4.QMessageBox.warning(
                    self, _("Error - Veusz"),
                    _("Unable to load default stylesheet '%s'\n\n%s") %
                    (filename, cstrerror(e)))
            else:
                # reset any modified flag
                self.document.setModified(False)
                self.document.changeset = 0

    def loadDefaultCustomDefinitions(self):
        """Loads the custom definitions for the new document."""
        filename = setdb['custom_default']
        if filename:
            try:
                self.document.applyOperation(
                    document.OperationLoadCustom(filename) )
            except EnvironmentError as e:
                qt4.QMessageBox.warning(
                    self, _("Error - Veusz"),
                    _("Unable to load custom definitions '%s'\n\n%s") %
                    (filename, cstrerror(e)))
            else:
                # reset any modified flag
                self.document.setModified(False)
                self.document.changeset = 0

    def slotAboutToShowEdit(self):
        """Enable/disable undo/redo menu items."""

        # enable distable, and add appropriate text to describe
        # the operation being undone/redone
        canundo = self.document.canUndo()
        undotext = _('Undo')
        if canundo:
            undotext = "%s %s" % (undotext, self.document.historyundo[-1].descr)
        self.vzactions['edit.undo'].setText(undotext)
        self.vzactions['edit.undo'].setEnabled(canundo)

        canredo = self.document.canRedo()
        redotext = _('Redo')
        if canredo:
            redotext = "%s %s" % (redotext, self.document.historyredo[-1].descr)
        self.vzactions['edit.redo'].setText(redotext)
        self.vzactions['edit.redo'].setEnabled(canredo)

    def slotEditUndo(self):
        """Undo the previous operation"""
        if self.document.canUndo():
            self.document.undoOperation()
        self.treeedit.checkWidgetSelected()

    def slotEditRedo(self):
        """Redo the previous operation"""
        if self.document.canRedo():
            self.document.redoOperation()

    def slotEditPreferences(self):
        dialog = PreferencesDialog(self)
        dialog.exec_()

    def slotEditStylesheet(self):
        dialog = StylesheetDialog(self, self.document)
        self.showDialog(dialog)
        return dialog

    def slotEditCustom(self):
        dialog = CustomDialog(self, self.document)
        self.showDialog(dialog)
        return dialog

    def definePlugins(self, pluginlist, actions, menuname):
        """Create menu items and actions for plugins.

        pluginlist: list of plugin classes
        actions: dict of actions to add new actions to
        menuname: string giving prefix for new menu entries (inside actions)
        """

        menu = []
        for pluginkls in pluginlist:
            def loaddialog(pluginkls=pluginkls):
                """Load plugin dialog"""
                handlePlugin(self, self.document, pluginkls)

            actname = menuname + '.' + '.'.join(pluginkls.menu)
            text = pluginkls.menu[-1]
            if pluginkls.has_parameters:
                text += '...'
            actions[actname] = utils.makeAction(
                self,
                pluginkls.description_short,
                text,
                loaddialog)

            # build up menu from tuple of names
            menulook = menu
            namebuild = [menuname]
            for cmpt in pluginkls.menu[:-1]:
                namebuild.append(cmpt)
                name = '.'.join(namebuild)

                for c in menulook:
                    if c[0] == name:
                        menulook = c[2]
                        break
                else:
                    menulook.append( [name, cmpt, []] )
                    menulook = menulook[-1][2]

            menulook.append(actname)

        return menu

    def _defineMenus(self):
        """Initialise the menus and toolbar."""

        # these are actions for main menu toolbars and menus
        a = utils.makeAction
        self.vzactions = {
            'file.new':
                a(self, _('New document'), _('&New'),
                  self.slotFileNew,
                  icon='kde-document-new', key='Ctrl+N'),
            'file.open':
                a(self, _('Open a document'), _('&Open...'),
                  self.slotFileOpen,
                  icon='kde-document-open', key='Ctrl+O'),
            'file.save':
                a(self, _('Save the document'), _('&Save'),
                  self.slotFileSave,
                  icon='kde-document-save', key='Ctrl+S'),
            'file.saveas':
                a(self, _('Save the current graph under a new name'),
                  _('Save &As...'), self.slotFileSaveAs,
                  icon='kde-document-save-as'),
            'file.print':
                a(self, _('Print the document'), _('&Print...'),
                  self.slotFilePrint,
                  icon='kde-document-print', key='Ctrl+P'),
            'file.export':
                a(self, _('Export the current page'), _('&Export...'),
                  self.slotFileExport,
                  icon='kde-document-export'),
            'file.close':
                a(self, _('Close current window'), _('Close Window'),
                  self.slotFileClose,
                  icon='kde-window-close', key='Ctrl+W'),
            'file.quit':
                a(self, _('Exit the program'), _('&Quit'),
                  self.slotFileQuit,
                  icon='kde-application-exit', key='Ctrl+Q'),

            'edit.undo':
                a(self, _('Undo the previous operation'), _('Undo'),
                  self.slotEditUndo,
                  icon='kde-edit-undo',  key='Ctrl+Z'),
            'edit.redo':
                a(self, _('Redo the previous operation'), _('Redo'),
                  self.slotEditRedo,
                  icon='kde-edit-redo', key='Ctrl+Shift+Z'),
            'edit.prefs':
                a(self, _('Edit preferences'), _('Preferences...'),
                  self.slotEditPreferences,
                  icon='veusz-edit-prefs'),
            'edit.custom':
                a(self, _('Edit custom functions and constants'),
                  _('Custom definitions...'),
                  self.slotEditCustom,
                  icon='veusz-edit-custom'),

            'edit.stylesheet':
                a(self,
                  _('Edit stylesheet to change default widget settings'),
                  _('Default styles...'),
                  self.slotEditStylesheet, icon='settings_stylesheet'),

            'view.edit':
                a(self, _('Show or hide edit window'), _('Edit window'),
                  None, checkable=True),
            'view.props':
                a(self, _('Show or hide property window'), _('Properties window'),
                  None, checkable=True),
            'view.format':
                a(self, _('Show or hide formatting window'), _('Formatting window'),
                  None, checkable=True),
            'view.console':
                a(self, _('Show or hide console window'), _('Console window'),
                  None, checkable=True),
            'view.datanav':
                a(self, _('Show or hide data navigator window'), _('Data navigator window'),
                  None, checkable=True),

            'view.maintool':
                a(self, _('Show or hide main toolbar'), _('Main toolbar'),
                  None, checkable=True),
            'view.datatool':
                a(self, _('Show or hide data toolbar'), _('Data toolbar'),
                  None, checkable=True),
            'view.viewtool':
                a(self, _('Show or hide view toolbar'), _('View toolbar'),
                  None, checkable=True),
            'view.edittool':
                a(self, _('Show or hide editing toolbar'), _('Editing toolbar'),
                  None, checkable=True),
            'view.addtool':
                a(self, _('Show or hide insert toolbar'), _('Insert toolbar'),
                  None, checkable=True),

            'data.import':
                a(self, _('Import data into Veusz'), _('&Import...'),
                  self.slotDataImport, icon='kde-vzdata-import'),
            'data.edit':
                a(self, _('Edit and enter new datasets'), _('&Editor...'),
                  self.slotDataEdit, icon='kde-edit-veuszedit'),
            'data.create':
                a(self, _('Create new datasets using ranges, parametrically or as functions of existing datasets'), _('&Create...'),
                  self.slotDataCreate, icon='kde-dataset-new-veuszedit'),
            'data.create2d':
                a(self, _('Create new 2D datasets from existing datasets, or as a function of x and y'), _('Create &2D...'),
                  self.slotDataCreate2D, icon='kde-dataset2d-new-veuszedit'),
            'data.capture':
                a(self, _('Capture remote data'), _('Ca&pture...'),
                  self.slotDataCapture, icon='veusz-capture-data'),
            'data.histogram':
                a(self, _('Histogram data'), _('&Histogram...'),
                  self.slotDataHistogram, icon='button_bar'),
            'data.reload':
                a(self, _('Reload linked datasets'), _('&Reload'),
                  self.slotDataReload, icon='kde-view-refresh'),

            'help.home':
                a(self, _('Go to the Veusz home page on the internet'),
                  _('Home page'), self.slotHelpHomepage),
            'help.project':
                a(self, _('Go to the Veusz project page on the internet'),
                  _('GNA Project page'), self.slotHelpProjectPage),
            'help.bug':
                a(self, _('Report a bug on the internet'),
                  _('Suggestions and bugs'), self.slotHelpBug),
            'help.tutorial':
                a(self, _('An interactive Veusz tutorial'),
                  _('Tutorial'), self.slotHelpTutorial),
            'help.about':
                a(self, _('Displays information about the program'), _('About...'),
                  self.slotHelpAbout, icon='veusz')
            }

        # create main toolbar
        tb = self.maintoolbar = qt4.QToolBar(_("Main toolbar - Veusz"), self)
        iconsize = setdb['toolbar_size']
        tb.setIconSize(qt4.QSize(iconsize, iconsize))
        tb.setObjectName('veuszmaintoolbar')
        self.addToolBar(qt4.Qt.TopToolBarArea, tb)
        utils.addToolbarActions(tb, self.vzactions,
                                ('file.new', 'file.open', 'file.save',
                                 'file.print', 'file.export'))

        # data toolbar
        tb = self.datatoolbar = qt4.QToolBar(_("Data toolbar - Veusz"), self)
        tb.setIconSize(qt4.QSize(iconsize, iconsize))
        tb.setObjectName('veuszdatatoolbar')
        self.addToolBar(qt4.Qt.TopToolBarArea, tb)
        utils.addToolbarActions(tb, self.vzactions,
                                ('data.import', 'data.edit',
                                 'data.create', 'data.capture',
                                 'data.reload'))

        # menu structure
        filemenu = [
            'file.new', 'file.open',
            ['file.filerecent', _('Open &Recent'), []],
            '',
            'file.save', 'file.saveas',
            '',
            'file.print', 'file.export',
            '',
            'file.close', 'file.quit'
            ]
        editmenu = [
            'edit.undo', 'edit.redo',
            '',
            ['edit.select', _('&Select'), []],
            '',
            'edit.prefs', 'edit.stylesheet', 'edit.custom',
            ''
            ]
        viewwindowsmenu = [
            'view.edit', 'view.props', 'view.format',
            'view.console', 'view.datanav',
            '',
            'view.maintool', 'view.viewtool',
            'view.addtool', 'view.edittool'
            ]
        viewmenu = [
            ['view.viewwindows', _('&Windows'), viewwindowsmenu],
            ''
            ]
        insertmenu = [
            ]

        # load dataset plugins and create menu
        datapluginsmenu = self.definePlugins( plugins.datasetpluginregistry,
                                              self.vzactions, 'data.ops' )

        datamenu = [
            ['data.ops', _('&Operations'), datapluginsmenu],
            'data.import', 'data.edit', 'data.create',
            'data.create2d', 'data.capture', 'data.histogram',
            'data.reload',
            ]
        helpmenu = [
            'help.home', 'help.project', 'help.bug',
            '',
            'help.tutorial',
            '',
            ['help.examples', _('&Example documents'), []],
            '',
            'help.about'
            ]

        # load tools plugins and create menu
        toolsmenu = self.definePlugins( plugins.toolspluginregistry,
                                        self.vzactions, 'tools' )

        menus = [
            ['file', _('&File'), filemenu],
            ['edit', _('&Edit'), editmenu],
            ['view', _('&View'), viewmenu],
            ['insert', _('&Insert'), insertmenu],
            ['data', _('&Data'), datamenu],
            ['tools', _('&Tools'), toolsmenu],
            ['help', _('&Help'), helpmenu],
            ]

        self.menus = {}
        utils.constructMenus(self.menuBar(), self.menus, menus, self.vzactions)

        self.populateExamplesMenu()

    def _setPickerFont(self, label):
        f = label.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.2)
        label.setFont(f)

    def populateExamplesMenu(self):
        """Add examples to help menu."""

        examples = glob.glob(os.path.join(utils.exampleDirectory, '*.vsz'))
        menu = self.menus["help.examples"]
        for ex in sorted(examples):
            name = os.path.splitext(os.path.basename(ex))[0]

            def _openexample(ex=ex):
                MainWindow.CreateWindow(ex)

            a = menu.addAction(name, _openexample)
            a.setStatusTip(_("Open %s example document") % name)

    def defineViewWindowMenu(self):
        """Setup View -> Window menu."""

        def viewHideWindow(window):
            """Toggle window visibility."""
            w = window
            def f():
                w.setVisible(not w.isVisible())
            return f

        # set whether windows are visible and connect up to toggle windows
        self.viewwinfns = []
        for win, act in ((self.treeedit, 'view.edit'),
                         (self.propdock, 'view.props'),
                         (self.formatdock, 'view.format'),
                         (self.console, 'view.console'),
                         (self.datadock, 'view.datanav'),
                         (self.maintoolbar, 'view.maintool'),
                         (self.datatoolbar, 'view.datatool'),
                         (self.treeedit.edittoolbar, 'view.edittool'),
                         (self.treeedit.addtoolbar, 'view.addtool'),
                         (self.plot.viewtoolbar, 'view.viewtool')):

            a = self.vzactions[act]
            fn = viewHideWindow(win)
            self.viewwinfns.append( (win, a, fn) )
            self.connect(a, qt4.SIGNAL('triggered()'), fn)

        # needs to update state every time menu is shown
        self.connect(self.menus['view.viewwindows'],
                     qt4.SIGNAL('aboutToShow()'),
                     self.slotAboutToShowViewWindow)

    def slotAboutToShowViewWindow(self):
        """Enable/disable View->Window item check boxes."""

        for win, act, fn in self.viewwinfns:
            act.setChecked(not win.isHidden())

    def showDialog(self, dialog):
        """Show dialog given."""
        self.connect(dialog, qt4.SIGNAL('dialogFinished'), self.deleteDialog)
        self.dialogs.append(dialog)
        dialog.show()
        self.emit( qt4.SIGNAL('dialogShown'), dialog )

    def deleteDialog(self, dialog):
        """Remove dialog from list of dialogs."""
        try:
            idx = self.dialogs.index(dialog)
            del self.dialogs[idx]
        except ValueError:
            pass

    def slotDataImport(self):
        """Display the import data dialog."""
        dialog = importdialog.ImportDialog(self, self.document)
        self.showDialog(dialog)
        return dialog

    def slotDataEdit(self, editdataset=None):
        """Edit existing datasets.

        If editdataset is set to a dataset name, edit this dataset
        """
        dialog = dataeditdialog.DataEditDialog(self, self.document)
        self.showDialog(dialog)
        if editdataset is not None:
            dialog.selectDataset(editdataset)
        return dialog

    def slotDataCreate(self):
        """Create new datasets."""
        dialog = DataCreateDialog(self, self.document)
        self.showDialog(dialog)
        return dialog

    def slotDataCreate2D(self):
        """Create new datasets."""
        dialog = DataCreate2DDialog(self, self.document)
        self.showDialog(dialog)
        return dialog

    def slotDataCapture(self):
        """Capture remote data."""
        dialog = CaptureDialog(self.document, self)
        self.showDialog(dialog)
        return dialog

    def slotDataHistogram(self):
        """Histogram data."""
        dialog = HistoDataDialog(self, self.document)
        self.showDialog(dialog)
        return dialog

    def slotDataReload(self):
        """Reload linked datasets."""
        dialog = ReloadData(self.document, self)
        self.showDialog(dialog)
        return dialog

    def slotHelpHomepage(self):
        """Go to the veusz homepage."""
        qt4.QDesktopServices.openUrl(qt4.QUrl('http://home.gna.org/veusz/'))

    def slotHelpProjectPage(self):
        """Go to the veusz project page."""
        qt4.QDesktopServices.openUrl(qt4.QUrl('http://gna.org/projects/veusz/'))

    def slotHelpBug(self):
        """Go to the veusz bug page."""
        qt4.QDesktopServices.openUrl(
            qt4.QUrl('https://gna.org/bugs/?group=veusz') )

    def askTutorial(self):
        """Ask if tutorial wanted."""
        retn = qt4.QMessageBox.question(
            self, _("Veusz Tutorial"),
            _("Veusz includes a tutorial to help get you started.\n"
              "Would you like to start the tutorial now?\n"
              "If not, you can access it later through the Help menu."),
            qt4.QMessageBox.Yes | qt4.QMessageBox.No
            )

        if retn == qt4.QMessageBox.Yes:
            self.slotHelpTutorial()

    def slotHelpTutorial(self):
        """Show a Veusz tutorial."""
        if self.document.isBlank():
            # run the tutorial
            from .tutorial import TutorialDock
            tutdock = TutorialDock(self.document, self, self)
            self.addDockWidget(qt4.Qt.RightDockWidgetArea, tutdock)
            tutdock.show()
        else:
            # open up a blank window for tutorial
            win = self.CreateWindow()
            win.slotHelpTutorial()

    def slotHelpAbout(self):
        """Show about dialog."""
        AboutDialog(self).exec_()

    def queryOverwrite(self):
        """Do you want to overwrite the current document.

        Returns qt4.QMessageBox.(Yes,No,Cancel)."""

        # include filename in mesage box if we can
        filetext = ''
        if self.filename:
            filetext = " '%s'" % os.path.basename(self.filename)

        # show message box
        mb = qt4.QMessageBox(_("Save file?"),
                             _("Document%s was modified. Save first?") % filetext,
                             qt4.QMessageBox.Warning,
                             qt4.QMessageBox.Yes | qt4.QMessageBox.Default,
                             qt4.QMessageBox.No,
                             qt4.QMessageBox.Cancel | qt4.QMessageBox.Escape,
                             self)
        mb.setButtonText(qt4.QMessageBox.Yes, _("&Save"))
        mb.setButtonText(qt4.QMessageBox.No, _("&Discard"))
        mb.setButtonText(qt4.QMessageBox.Cancel, _("&Cancel"))
        return mb.exec_()

    def closeEvent(self, event):
        """Before closing, check whether we need to save first."""

        # if the document has been modified then query user for saving
        if self.document.isModified():
            v = self.queryOverwrite()
            if v == qt4.QMessageBox.Cancel:
                event.ignore()
                return
            elif v == qt4.QMessageBox.Yes:
                self.slotFileSave()

        # store working directory
        setdb['dirname'] = self.dirname
        setdb['dirname_export'] = self.dirname_export

        # store the current geometry in the settings database
        geometry = ( self.x(), self.y(), self.width(), self.height() )
        setdb['geometry_mainwindow'] = geometry

        # store docked windows
        data = str(self.saveState())
        setdb['geometry_mainwindowstate'] = data

        # save current setting db
        setdb.writeSettings()

        event.accept()

    def setupWindowGeometry(self):
        """Restoring window geometry if possible."""

        # count number of main windows shown
        nummain = 0
        for w in qt4.qApp.topLevelWidgets():
            if isinstance(w, qt4.QMainWindow):
                nummain += 1

        # if we can restore the geometry, do so
        if 'geometry_mainwindow' in setdb:
            geometry = setdb['geometry_mainwindow']
            self.resize( qt4.QSize(geometry[2], geometry[3]) )
            if nummain <= 1:
                self.move( qt4.QPoint(geometry[0], geometry[1]) )

        # restore docked window geometry
        if 'geometry_mainwindowstate' in setdb:
            b = qt4.QByteArray(setdb['geometry_mainwindowstate'])
            self.restoreState(b)

    def slotFileNew(self):
        """New file."""
        self.CreateWindow()

    def slotFileSave(self):
        """Save file."""

        if self.filename == '':
            self.slotFileSaveAs()
        else:
            # show busy cursor
            qt4.QApplication.setOverrideCursor( qt4.QCursor(qt4.Qt.WaitCursor) )
            try:
                ofile = open(self.filename, 'w')
                self.document.saveToFile(ofile)
                self.updateStatusbar(_("Saved to %s") % self.filename)
            except EnvironmentError as e:
                qt4.QApplication.restoreOverrideCursor()
                qt4.QMessageBox.critical(
                    self, _("Error - Veusz"),
                    _("Unable to save document as '%s'\n\n%s") %
                    (self.filename, cstrerror(e)))
            else:
                # restore the cursor
                qt4.QApplication.restoreOverrideCursor()

    def updateTitlebar(self):
        """Put the filename into the title bar."""
        if self.filename == '':
            self.setWindowTitle(_('Untitled - Veusz'))
        else:
            self.setWindowTitle( _("%s - Veusz") %
                                 os.path.basename(self.filename) )

    def plotQueueChanged(self, incr):
        self.plotqueuecount += incr
        text = u'•' * self.plotqueuecount
        self.plotqueuelabel.setText(text)

    def _fileSaveDialog(self, filetype, filedescr, dialogtitle):
        """A generic file save dialog for exporting / saving."""

        fd = qt4.QFileDialog(self, dialogtitle)
        fd.setDirectory(self.dirname)
        fd.setFileMode( qt4.QFileDialog.AnyFile )
        fd.setAcceptMode( qt4.QFileDialog.AcceptSave )
        fd.setFilter( "%s (*.%s)" % (filedescr, filetype) )

        # okay was selected (and is okay to overwrite if it exists)
        if fd.exec_() == qt4.QDialog.Accepted:
            # save directory for next time
            self.dirname = fd.directory().absolutePath()
            # update the edit box
            filename = fd.selectedFiles()[0]
            if os.path.splitext(filename)[1] == '':
                filename += '.' + filetype

            return filename
        return None

    def _fileOpenDialog(self, filetype, filedescr, dialogtitle):
        """Display an open dialog and return a filename."""

        fd = qt4.QFileDialog(self, dialogtitle)
        fd.setDirectory(self.dirname)
        fd.setFileMode( qt4.QFileDialog.ExistingFile )
        fd.setAcceptMode( qt4.QFileDialog.AcceptOpen )
        fd.setFilter( "%s (*.%s)" % (filedescr, filetype) )

        # if the user chooses a file
        if fd.exec_() == qt4.QDialog.Accepted:
            # save directory for next time
            self.dirname = fd.directory().absolutePath()

            filename = fd.selectedFiles()[0]
            try:
                open(filename)
            except EnvironmentError as e:
                qt4.QMessageBox.critical(
                    self, _("Error - Veusz"),
                    _("Unable to open '%s'\n\n%s") %
                    (filename, cstrerror(e)))
                return None
            return filename
        return None

    def slotFileSaveAs(self):
        """Save As file."""

        filename = self._fileSaveDialog('vsz', _('Veusz script files'), _('Save as'))
        if filename:
            self.filename = filename
            self.updateTitlebar()

            self.slotFileSave()

    def openFile(self, filename):
        """Select whether to load the file in the
        current window or in a blank window and calls the appropriate loader"""

        if self.document.isBlank():
            # If the file is new and there are no modifications,
            # reuse the current window
            self.openFileInWindow(filename)
        else:
            # create a new window
            self.CreateWindow(filename)

    class _unsafeCmdMsgBox(qt4.QMessageBox):
        """Show document is unsafe."""
        def __init__(self, window, filename):
            qt4.QMessageBox.__init__(self, _("Unsafe code in document"),
                                     _("The document '%s' contains potentially "
                                       "unsafe code which may damage your "
                                       "computer or data. Please check that the "
                                       "file comes from a "
                                       "trusted source.") % filename,
                                     qt4.QMessageBox.Warning,
                                     qt4.QMessageBox.Yes,
                                     qt4.QMessageBox.No | qt4.QMessageBox.Default,
                                     qt4.QMessageBox.NoButton,
                                     window)
            self.setButtonText(qt4.QMessageBox.Yes, _("C&ontinue anyway"))
            self.setButtonText(qt4.QMessageBox.No, _("&Stop loading"))

    class _unsafeVeuszCmdMsgBox(qt4.QMessageBox):
        """Show document has unsafe Veusz commands."""
        def __init__(self, window):
            qt4.QMessageBox.__init__(self, _('Unsafe Veusz commands'),
                                     _('This Veusz document contains potentially'
                                       ' unsafe Veusz commands for Saving, '
                                       'Exporting or Printing. Please check that the'
                                       ' file comes from a trusted source.'),
                                     qt4.QMessageBox.Warning,
                                     qt4.QMessageBox.Yes,
                                     qt4.QMessageBox.No | qt4.QMessageBox.Default,
                                     qt4.QMessageBox.NoButton,
                                     window)
            self.setButtonText(qt4.QMessageBox.Yes, _("C&ontinue anyway"))
            self.setButtonText(qt4.QMessageBox.No, _("&Ignore command"))

    def openFileInWindow(self, filename):
        """Actually do the work of loading a new document.
        """

        # FIXME: This function suffers from spaghetti code
        # it needs splitting up into bits to make it clearer
        # the steps are fairly well documented below, however
        #####################################################

        qt4.QApplication.setOverrideCursor( qt4.QCursor(qt4.Qt.WaitCursor) )

        # read script
        try:
            script = open(filename, 'rU').read()
        except EnvironmentError as e:
            qt4.QApplication.restoreOverrideCursor()
            qt4.QMessageBox.critical(
                self, _("Error - Veusz"),
                _("Cannot open document '%s'\n\n%s") %
                (filename, cstrerror(e)))
            self.setupDefaultDoc()
            return

        def errordialog(e):
            # display error dialog if there is an error loading
            qt4.QApplication.restoreOverrideCursor()
            i = sys.exc_info()
            backtrace = traceback.format_exception( *i )
            d = ErrorLoadingDialog(self, filename, str(e), ''.join(backtrace))
            d.exec_()

        # compile script and check for security (if reqd)
        unsafe = setting.transient_settings['unsafe_mode']
        while True:
            try:
                compiled = utils.compileChecked(script, mode='exec', filename=filename,
                                                ignoresecurity=unsafe)
                break
            except utils.SafeEvalException:
                # ask the user whether to execute in unsafe mode
                qt4.QApplication.restoreOverrideCursor()
                if ( self._unsafeCmdMsgBox(self, filename).exec_() ==
                     qt4.QMessageBox.No ):
                    return
                unsafe = True
            except Exception as e:
                errordialog(e)
                return

        # set up environment to run script
        env = self.document.eval_context.copy()
        interface = document.CommandInterface(self.document)

        # allow safe commands as-is
        for cmd in interface.safe_commands:
            env[cmd] = getattr(interface, cmd)

        # define root node
        env['Root'] = interface.Root

        # wrap "unsafe" commands with a message box to check the user
        safenow = [unsafe]
        def _unsafeCaller(func):
            def wrapped(*args, **argsk):
                if not safenow[0]:
                    qt4.QApplication.restoreOverrideCursor()
                    if ( self._unsafeVeuszCmdMsgBox(self).exec_() ==
                         qt4.QMessageBox.No ):
                        return
                safenow[0] = True
                func(*args, **argsk)
            return wrapped
        for name in interface.unsafe_commands:
            env[name] = _unsafeCaller(getattr(interface, name))

        # save stdout and stderr, then redirect to console
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout = self.console.con_stdout
        sys.stderr = self.console.con_stderr

        # get ready to load document
        env['__file__'] = os.path.abspath(filename)
        self.document.wipe()
        self.document.suspendUpdates()

        # allow import to happen relative to loaded file
        interface.AddImportPath( os.path.dirname(os.path.abspath(filename)) )

        try:
            # actually run script text
            cexec(compiled, env)
        except Exception as e:
            # need to remember to restore stdout, stderr
            sys.stdout, sys.stderr = stdout, stderr
            self.document.enableUpdates()
            errordialog(e)
            return

        # need to remember to restore stdout, stderr
        sys.stdout, sys.stderr = stdout, stderr

        # document is loaded
        self.document.enableUpdates()
        self.document.setModified(False)
        self.document.clearHistory()

        # remember file for recent list
        self.addRecentFile(filename)

        # let the main window know
        self.filename = filename
        self.updateTitlebar()
        self.updateStatusbar(_("Opened %s") % filename)

        # use current directory of file if not using cwd mode
        if not setdb['dirname_usecwd']:
            self.dirname = os.path.dirname( os.path.abspath(filename) )
            self.dirname_export = self.dirname

        # notify cmpts which need notification that doc has finished opening
        self.emit(qt4.SIGNAL("documentopened"))
        qt4.QApplication.restoreOverrideCursor()

    def addRecentFile(self, filename):
        """Add a file to the recent files list."""

        recent = setdb['main_recentfiles']
        filename = os.path.abspath(filename)

        if filename in recent:
            del recent[recent.index(filename)]
        recent.insert(0, filename)
        setdb['main_recentfiles'] = recent[:10]
        self.populateRecentFiles()

    def slotFileOpen(self):
        """Open an existing file in a new window."""

        filename = self._fileOpenDialog('vsz', _('Veusz script files'), _('Open'))
        if filename:
            self.openFile(filename)

    def populateRecentFiles(self):
        """Populate the recently opened files menu with a list of
        recently opened files"""

        menu = self.menus["file.filerecent"]
        menu.clear()

        newMenuItems = []
        if setdb['main_recentfiles']:
            files = [f for f in setdb['main_recentfiles']
                     if os.path.isfile(f)]
            self._openRecentFunctions = []

            # add each recent file to menu
            for i, path in enumerate(files):

                def fileOpener(filename=path):
                    self.openFile(filename)

                self._openRecentFunctions.append(fileOpener)
                newMenuItems.append(('filerecent%i' % i, _('Open File %s') % path,
                                     os.path.basename(path),
                                     'file.filerecent', fileOpener,
                                     '', False, ''))

            menu.setEnabled(True)
            self.recentFileActions = utils.populateMenuToolbars(
                newMenuItems, self.maintoolbar, self.menus)
        else:
            menu.setEnabled(False)

    def slotFileExport(self):
        """Export the graph."""

        # check there is a page
        if self.document.getNumberPages() == 0:
            qt4.QMessageBox.warning(self, _("Error - Veusz"),
                                    _("No pages to export"))
            return

        # File types we can export to in the form ([extensions], Name)
        fd = qt4.QFileDialog(self, _('Export page'))
        fd.setDirectory( self.dirname_export )

        fd.setFileMode( qt4.QFileDialog.AnyFile )
        fd.setAcceptMode( qt4.QFileDialog.AcceptSave )

        # Create a mapping between a format string and extensions
        filtertoext = {}
        # convert extensions to filter
        exttofilter = {}
        filters = []
        # a list of extensions which are allowed
        validextns = []
        formats = document.Export.formats
        for extns, name in formats:
            extensions = " ".join(["*." + item for item in extns])
            # join eveything together to make a filter string
            filterstr = '%s (%s)' % (name, extensions)
            filtertoext[filterstr] = extns
            for e in extns:
                exttofilter[e] = filterstr
            filters.append(filterstr)
            validextns += extns
        fd.setNameFilters(filters)

        # restore last format if possible
        try:
            filt = setdb['export_lastformat']
            fd.selectNameFilter(filt)
            extn = formats[filters.index(filt)][0][0]
        except (KeyError, IndexError, ValueError):
            extn = 'pdf'
            fd.selectNameFilter( exttofilter[extn] )

        if self.filename:
            # try to convert current filename to export name
            filename = os.path.basename(self.filename)
            filename = os.path.splitext(filename)[0] + '.' + extn
            fd.selectFile(filename)

        if fd.exec_() == qt4.QDialog.Accepted:
            # save directory for next time
            self.dirname_export = fd.directory().absolutePath()

            filterused = str(fd.selectedFilter())
            setdb['export_lastformat'] = filterused

            chosenextns = filtertoext[filterused]

            # show busy cursor
            qt4.QApplication.setOverrideCursor( qt4.QCursor(qt4.Qt.WaitCursor) )

            filename = fd.selectedFiles()[0]

            # Add a default extension if one isn't supplied
            # this is the extension without the dot
            ext = os.path.splitext(filename)[1][1:]
            if (ext not in validextns) and (ext not in chosenextns):
                filename += "." + chosenextns[0]

            export = document.Export(
                self.document,
                filename,
                self.plot.getPageNumber(),
                bitmapdpi=setdb['export_DPI'],
                pdfdpi=setdb['export_DPI_PDF'],
                antialias=setdb['export_antialias'],
                color=setdb['export_color'],
                quality=setdb['export_quality'],
                backcolor=setdb['export_background'],
                svgtextastext=setdb['export_SVG_text_as_text'],
                )

            try:
                export.export()
            except (RuntimeError, EnvironmentError) as e:
                if isinstance(e, EnvironmentError):
                    msg = cstrerror(e)
                else:
                    msg = cstr(e)

                qt4.QApplication.restoreOverrideCursor()
                qt4.QMessageBox.critical(
                    self, _("Error - Veusz"),
                    _("Error exporting to file '%s'\n\n%s") %
                    (filename, msg))
            else:
                qt4.QApplication.restoreOverrideCursor()

    def slotFilePrint(self):
        """Print the document."""
        document.printDialog(self, self.document, filename=self.filename)

    def slotModifiedDoc(self, ismodified):
        """Disable certain actions if document is not modified."""

        # enable/disable file, save menu item
        self.vzactions['file.save'].setEnabled(ismodified)

    def slotFileClose(self):
        """File close window chosen."""
        self.close()

    def slotFileQuit(self):
        """File quit chosen."""
        qt4.qApp.closeAllWindows()

    def slotUpdatePage(self, number):
        """Update page number when the plot window says so."""

        np = self.document.getNumberPages()
        if np == 0:
            self.pagelabel.setText(_("No pages"))
        else:
            self.pagelabel.setText(_("Page %i/%i") % (number+1, np))

    def slotUpdateAxisValues(self, values):
        """Update the position where the mouse is relative to the axes."""

        if values:
            # construct comma separated text representing axis values
            valitems = []
            for name, val in citems(values):
                valitems.append('%s=%#.4g' % (name, val))
            valitems.sort()
            self.axisvalueslabel.setText(', '.join(valitems))
        else:
            self.axisvalueslabel.setText(_('No position'))

    def slotPickerEnabled(self, enabled):
        if enabled:
            self.pickerlabel.setText(_('No point selected'))
            self.pickerlabel.show()
        else:
            self.pickerlabel.hide()

    def slotUpdatePickerLabel(self, info):
        """Display the picked point"""
        xv, yv = info.coords
        xn, yn = info.labels
        xt, yt = info.displaytype
        ix = str(info.index)
        if ix:
            ix = '[' + ix + ']'

        # format values for display
        def fmt(val, dtype):
            if dtype == 'date':
                return utils.dateFloatToString(val)
            elif dtype == 'numeric':
                return '%0.5g' % val
            elif dtype == 'text':
                return val
            else:
                raise RuntimeError

        xtext = fmt(xv, xt)
        ytext = fmt(yv, yt)

        t = '%s: %s%s = %s, %s%s = %s' % (
                info.widget.name, xn, ix, xtext, yn, ix, ytext)
        self.pickerlabel.setText(t)
        if setdb['picker_to_console']:
            self.console.appendOutput(t + "\n", 'error')
        if setdb['picker_to_clipboard']:
            clipboard = qt4.QApplication.clipboard()
            if clipboard.mimeData().hasText():
                clipboard.setText(clipboard.text()+"\n"+t)
            else:
                qt4.QApplication.clipboard().setText(t)

    def slotAllowedImportsDoc(self, module, names):
        """Are allowed imports?"""

        d = SafetyImportDialog(self, module, names)
        d.exec_()
