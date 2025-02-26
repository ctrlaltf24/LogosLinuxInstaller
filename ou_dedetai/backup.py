import queue
import logging
import shutil
import time
from pathlib import Path
from ou_dedetai import constants
from ou_dedetai import utils
from ou_dedetai.app import App


class BackupBase:
    DATA_DIRS = ['Data', 'Documents', 'Users']

    def __init__(self, app: App):
        self.app = app
        self.mode = None
        self.source_dir = None
        self.data_size = None
        self.destination_dir = None
        self.destination_disk_used_init = None
        self.q: queue.Queue[int] = queue.Queue()
        if not self.app.approve(f"Use existing backups folder \"{self.app.conf.backup_dir}\"?"): # noqa: E501
            # Reset backup dir.
            # The app will re-prompt next time the backup_dir is accessed
            app.conf._raw.backup_dir = None
        self.backup_dir = Path(self.app.conf.backup_dir).expanduser().resolve()
        try:
            self.backup_dir.mkdir(exist_ok=True, parents=True)
        except PermissionError:
            m = f"Folder not accessible: {self.backup_dir}"
            if constants.RUNMODE == 'snap':
                m += f"{m}\nTry connecting removable media:\nsnap connect {constants.BINARY_NAME}:removable-media"
            self.app.exit(m)

    def _copy_dirs(self, src_dirs, dst_dir):
        # logging.debug("starting _copy_dirs")
        for src in src_dirs:
            if not isinstance(src, Path):
                src = Path(src)
            logging.debug(f"copying \"{src}\" to \"{dst_dir}/{src.name}\"")
            shutil.copytree(src, Path(dst_dir) / src.name)

    def _get_copy_percentage(self):
        delta = self._get_dest_disk_used() - self.destination_disk_used_init
        percent = int(delta * 100 / self.data_size)
        # logging.debug(f"{percent=}")
        return percent

    def _get_dest_disk_used(self):
        return shutil.disk_usage(self.destination_dir).used

    def _get_dir_group_size(self, dirs):
        self.app.status("Calculating backup size…")
        self.app.start_thread(utils.get_folder_group_size, dirs, self.q)
        size = self.q.get()
        logging.debug(f"{size=}")
        return size

    def _get_source_subdirs(self):
        dirs = [self.source_dir / d for d in self.DATA_DIRS if (self.source_dir / d).is_dir()]  # noqa: E501
        if not dirs:
            self.app.exit(f"No files to {self.mode}")
        return dirs

    def _prepare_dest_dir(self):
        """Remove existing data."""
        for d in self.DATA_DIRS:
            dst = Path(self.destination_dir) / d
            if dst.is_dir():
                shutil.rmtree(dst)

    def _run(self):
        if self.source_dir is None:
            self.app.exit("Source not set")
        elif self.destination_dir is None:
            self.app.exit("Destination not set")
        src_dirs = self._get_source_subdirs()

        self.data_size = self._get_dir_group_size(src_dirs)
        self._prepare_dest_dir()
        self.destination_disk_used_init = self._get_dest_disk_used()
        self._verify_disk_space()
        logging.debug("starting data copy")
        t = self.app.start_thread(self._copy_dirs, src_dirs, self.destination_dir)
        try:
            while t.is_alive():
                self.app.status("copying…\r", self._get_copy_percentage())
                time.sleep(0.5)
            print()
        except KeyboardInterrupt:
            print()
            self.app.exit("Cancelled with Ctrl+C.")
        t.join()
        logging.debug("finished data copy")
        m = f"Finished {self.mode}. {self.data_size} bytes copied to {self.destination_dir}"  # noqa: E501
        self.app.status(m)

    def _verify_disk_space(self):
        if not utils.enough_disk_space(self.destination_dir, self.data_size):
            # self.destination_dir.rmdir()
            self.app.exit(f"Not enough free disk space for {self.mode}.")
        logging.debug(f"Sufficient space verified on {self.destination_dir} disk.")


class BackupTask(BackupBase):
    def __init__(self, app: App):
        super().__init__(app)
        self.mode = 'backup'
        self.description = 'Use'
        self.source_dir = Path(self.app.conf._logos_appdata_dir).expanduser().resolve()

    def run(self):
        """Run the backup task."""
        self._set_dest_dir()
        self.app.status(f"Backing up data to {self.destination_dir}…")
        self._run()

    def _set_dest_dir(self):
        timestamp = utils.get_timestamp().replace('-', '')
        name = f"{self.app.conf.faithlife_product}-{timestamp}"
        self.destination_dir = self.backup_dir / name
        logging.debug(f"Backup directory path: \"{self.destination_dir}\".")

        # Check for existing backup.
        try:
            self.destination_dir.mkdir()
        except FileExistsError:
            # This shouldn't happen, there is a timestamp in the backup_dir name
            self.app.exit(f"Backup already exists: {self.destination_dir}.")


class RestoreTask(BackupBase):
    def __init__(self, app: App):
        super().__init__(app)
        self.mode = 'restore'

    def run(self):
        """Run the restore task."""
        if self.source_dir is None:
            self._set_source_dir()
        self._set_dest_dir()

        self.app.status(f"Restoring backup from {self.source_dir}")
        self._run()

    def set_source_dir(self, src_dir: Path | str):
        """Explicitly set dir path to restore from"""
        self._set_source_dir(src_dir)

    def _set_dest_dir(self):
        # TODO: Use faithlife_product_name here?
        if not self.app.conf.logos_exe:
            self.app.exit("Cannot restore, Logos is not installed")
        self.destination_dir = Path(self.app.conf.logos_exe).parent

    def _set_source_dir(self, src_dir: Path = None):
        if src_dir is None:
            src_dir = utils.get_latest_folder(self.backup_dir)
            # FIXME: Shouldn't this prompt this prompt the list of backups?
            # Rather than forcing the latest
            # Offer to restore the most recent backup.
            if not self.app.approve(f"Restore most-recent backup?: {src_dir}", ""):  # noqa: E501
                # Reset and re-prompt
                self.app.conf._raw.backup_dir = None
                src_dir = utils.get_latest_folder(self.backup_dir)
        else:
            if not isinstance(src_dir, Path):
                src_dir = Path(src_dir)
        self.source_dir = src_dir


def backup(app: App):
    backup = BackupTask(app)
    backup.run()


def restore(app: App):
    restore = RestoreTask(app)
    restore.run()
