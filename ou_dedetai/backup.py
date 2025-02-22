import queue
import logging
import shutil
import time
from pathlib import Path
from ou_dedetai import utils
from ou_dedetai.app import App


def backup(app: App):
    backup_and_restore(mode='backup', app=app)


def restore(app: App):
    backup_and_restore(mode='restore', app=app)


def copy_data(src_dirs, dst_dir):
    for src in src_dirs:
        shutil.copytree(src, Path(dst_dir) / src.name)


# FIXME: almost seems like this is long enough to reuse the install_step count in app
# for a more detailed progress bar
def backup_and_restore(mode: str, app: App):
    app.status(f"Starting {mode}…")
    data_dirs = ['Data', 'Documents', 'Users']
    backup_dir = Path(app.conf.backup_dir).expanduser().resolve()

    verb = 'Use' if mode == 'backup' else 'Restore backup from'
    if not app.approve(f"{verb} existing backups folder \"{app.conf.backup_dir}\"?"): #noqa: E501
        # Reset backup dir.
        # The app will re-prompt next time the backup_dir is accessed
        app.conf._raw.backup_dir = None

    # Set source folders.
    backup_dir = Path(app.conf.backup_dir)
    try:
        backup_dir.mkdir(exist_ok=True, parents=True)
    except PermissionError:
        verb = 'access'
        if mode == 'backup':
            verb = 'create'
        app.exit(f"Can't {verb} folder: {backup_dir}")

    if mode == 'restore':
        restore_dir = utils.get_latest_folder(app.conf.backup_dir)
        restore_dir = Path(restore_dir).expanduser().resolve()
        # FIXME: Shouldn't this prompt this prompt the list of backups?
        # Rather than forcing the latest
        # Offer to restore the most recent backup.
        if not app.approve(f"Restore most-recent backup?: {restore_dir}", ""):  # noqa: E501
            # Reset and re-prompt
            app.conf._raw.backup_dir = None
            restore_dir = utils.get_latest_folder(app.conf.backup_dir)
            restore_dir = Path(restore_dir).expanduser().resolve()
        source_dir_base = restore_dir
    else:
        if not app.conf._logos_appdata_dir:
            app.exit("Cannot backup, Logos installation not found")
        source_dir_base = Path(app.conf._logos_appdata_dir)
    src_dirs = [source_dir_base / d for d in data_dirs if Path(source_dir_base / d).is_dir()]  # noqa: E501
    logging.debug(f"{src_dirs=}")
    if not src_dirs:
        app.exit(f"No files to {mode}")

    if mode == 'backup':
        app.status("Backing up data…")
    else:
        app.status("Restoring data…")

    # Get source transfer size.
    q: queue.Queue[int] = queue.Queue()
    message = "Calculating backup size…"
    app.status(message)
    i = 0
    t = app.start_thread(utils.get_folder_group_size, src_dirs, q)
    try:
        while t.is_alive():
            i += 1
            i = i % 20
            app.status(f"{message}{"." * i}\r")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        app.exit("Cancelled with Ctrl+C.")
    t.join()
    src_size = q.get()
    if src_size == 0:
        app.exit(f"Nothing to {mode}!")

    # Set destination folder.
    if mode == 'restore':
        if not app.conf.logos_exe:
            app.exit("Cannot restore, Logos is not installed")
        dst_dir = Path(app.conf.logos_exe).parent
        # Remove existing data.
        for d in data_dirs:
            dst = Path(dst_dir) / d
            if dst.is_dir():
                shutil.rmtree(dst)
    else:  # backup mode
        timestamp = utils.get_timestamp().replace('-', '')
        current_backup_name = f"{app.conf.faithlife_product}{app.conf.faithlife_product_version}-{timestamp}"  # noqa: E501
        dst_dir = backup_dir / current_backup_name
        logging.debug(f"Backup directory path: \"{dst_dir}\".")

        # Check for existing backup.
        try:
            dst_dir.mkdir()
        except FileExistsError:
            # This shouldn't happen, there is a timestamp in the backup_dir name
            app.exit(f"Backup already exists: {dst_dir}.")

    # Verify disk space.
    if not utils.enough_disk_space(dst_dir, src_size):
        dst_dir.rmdir()
        app.exit(f"Not enough free disk space for {mode}.")

    # Run file transfer.
    if mode == 'restore':
        m = f"Restoring backup from {str(source_dir_base)}…"
    else:
        m = f"Backing up to {str(dst_dir)}…"
    app.status(m)
    t = app.start_thread(copy_data, src_dirs, dst_dir)
    try:
        counter = 0
        while t.is_alive():
            logging.debug(f"DEV: Still copying… {counter}")
            counter = counter + 1
            time.sleep(1)
        print()
    except KeyboardInterrupt:
        print()
        app.exit("Cancelled with Ctrl+C.")
    t.join()
    app.status(f"Finished {mode}. {src_size} bytes copied to {str(dst_dir)}")