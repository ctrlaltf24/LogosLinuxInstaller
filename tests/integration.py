"""Basic implementations of some rudimentary tests

Should be migrated into unittests once that branch is merged
"""
# FIXME: refactor into unittests

import os
from pathlib import Path
import psutil
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Optional

REPOSITORY_ROOT_PATH = Path(__file__).parent.parent

class CommandFailedError(Exception):
    """Command Failed to execute"""
    command: list[str]
    stdout: str
    stderr: str

    def __init__(self, args: list[str], stdout: str, stderr: str):
        return super().__init__((
            f"Failed to execute: {" ".join(args)}:\n\n"
            f"stdout:\n{stdout}\n\n"
            f"stderr:\n{stderr}\n\n"
            "Command Failed. See above for details."
        ))


class TestFailed(Exception):
    pass

def run_cmd(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    """Wrapper around subprocess.run that:
    - captures stdin/stderr
    - sets text mode
    - checks returncode before returning

    All other args are passed through to subprocess.run
    """
    if "stdout" not in kwargs:
        kwargs["stdout"] = subprocess.PIPE
    if "stderr" not in kwargs:
        kwargs["stderr"] = subprocess.PIPE
    kwargs["text"] = True
    output = subprocess.run(*args, **kwargs)
    try:
        output.check_returncode()
    except subprocess.CalledProcessError as e:
        raise CommandFailedError(
            args=list(*args),
            stderr=output.stderr,
            stdout=output.stdout
        ) from e
    return output

class OuDedetai:
    _binary: Optional[str] = None
    _temp_dir: Optional[str] = None
    config: Optional[Path] = None
    install_dir: Optional[Path] = None
    log_level: str
    """Log level. One of:
    - quiet - warn+ - status
    - normal - warn+
    - verbose - info+
    - debug - debug
    """


    def __init__(self, isolate: bool = True, log_level: str = "quiet"):
        if isolate:
            self.isolate_files()
        self.log_level = log_level

    def isolate_files(self):
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir)
        # XXX: this isn't properly cleaned up. Context manager?
        self._temp_dir = tempfile.mkdtemp()
        self.config = Path(self._temp_dir) / "config.json"
        self.install_dir = Path(self._temp_dir) / "install_dir"

    @classmethod
    def _source_last_update(cls) -> float:
        """Last updated time of any source code in seconds since epoch"""
        path = REPOSITORY_ROOT_PATH / "ou_dedetai"
        output: float = 0
        for root, _, files in os.walk(path):
            for file in files:
                file_m = os.stat(Path(root) / file).st_mtime
                if file_m > output:
                    output = file_m
        return output

    @classmethod
    def _oudedetai_binary(cls) -> str:
        """Return the path to the binary"""
        output = REPOSITORY_ROOT_PATH / "dist" / "oudedetai"
        # First check to see if we need to build.
        # If either the file doesn't exist, or it was last modified earlier than
        # the source code, rebuild.
        if (
            not output.exists()
            or cls._source_last_update() > os.stat(str(output)).st_mtime
        ):
            print("Building binary…")
            if output.exists():
                os.remove(str(output))
            run_cmd(f"{REPOSITORY_ROOT_PATH / "scripts" / "build-binary.sh"}")

            if not output.exists():
                raise Exception("Build process failed to yield binary")
            print("Built binary.")

        return str(output)

    def run(self, *args, **kwargs):
        if self._binary is None:
            self._binary = self._oudedetai_binary()
        if "env" not in kwargs:
            kwargs["env"] = {}
        env: dict[str, str] = {}
        if self.config:
            env["CONFIG_FILE"] = str(self.config)
        if self.install_dir:
            env["INSTALLDIR"] = str(self.install_dir)
        env["PATH"] = os.environ.get("PATH", "")
        env["HOME"] = os.environ.get("HOME", "")
        env["DISPLAY"] = os.environ.get("DISPLAY", "")
        kwargs["env"] = env
        log_level = ""
        if self.log_level == "debug":
            log_level = "--debug"
        elif self.log_level == "verbose":
            log_level = "--verbose"
        elif self.log_level == "quiet":
            log_level = "--quiet"
        args = ([self._binary, log_level, "--i-agree-to-faithlife-terms"] + args[0], *args[1:]) #noqa: E501
        # FIXME: Output to both stdout and PIPE (for debugging these tests)
        output = run_cmd(*args, **kwargs)

        # FIXME: Test to make sure there is no stderr output either - AKA no warnings
        # if output.stderr:
        #     raise CommandFailedError(
        #         args[0],
        #         stdout=output.stdout,
        #         stderr=output.stderr
        #     )
        return output

    def uninstall(self):
        try:
            self.stop_app()
        except Exception:
            pass
        # XXX: Ideally the uninstall operation would automatically stop the app.
        # Open an issue for this.
        self.run(["--uninstall", "-y"])

    def start_app(self):
        # Start a thread, as this command doesn't exit
        threading.Thread(target=self.run, args=[["--run-installed-app"]]).start()
        # Now wait for the window to open.
        wait_for_logos_to_open()

    def stop_app(self):
        self.run(["--stop-installed-app"])
        # FIXME: wait for close?


def wait_for_true(
    callable: Callable[[], Optional[bool]],
    timeout: Optional[int] = 10,
    period: float = .1
) -> bool:
    exception = None
    start_time = time.time()
    while timeout is None or time.time() - start_time < timeout:
        try:
            if callable():
                return True
        except Exception as e:
            exception = e
        time.sleep(period)
    if exception:
        raise exception
    raise TimeoutError


def wait_for_window(window_name: str, timeout: int = 10):
    """Waits for an Xorg window to open, raises exception if it doesn't"""
    def _window_open():
        output = run_cmd(["xwininfo", "-tree", "-root"])
        if output.stderr:
            raise Exception(f"xwininfo failed: {output.stdout}\n{output.stderr}")
        if window_name not in output.stdout:
            raise Exception(f"Could not find {window_name} in {output.stdout}")
        return True
    wait_for_true(_window_open, timeout=timeout)


def wait_for_logos_to_open(timeout: int = 10) -> None:
    """Raises an exception if Logos isn't open"""
    # Check with Xorg to see if there is a window running with the string logos.exe
    wait_for_window("logos.exe", timeout=timeout)


def wait_for_directory_to_be_untouched(directory: str, period: float):
    def _check_for_directory_to_be_untouched():
        highest_modified_time: float = 0
        for dirpath, _, filenames in os.walk(directory):
            for filename in filenames:
                file_mtime = (Path(dirpath) / filename).stat().st_mtime
                if file_mtime > highest_modified_time:
                    highest_modified_time = file_mtime
        current_time = time.time()

        if (current_time - highest_modified_time) > period:
            return True
        else:
            return False

    wait_for_true(_check_for_directory_to_be_untouched, timeout=None, period=period / 2)


def test_run(ou_dedetai: OuDedetai):
    ou_dedetai.run(["--stop-installed-app"])

    # First launch Run the app. This assumes that logos is spawned before this completes
    ou_dedetai.run(["--run-installed-app"])

    wait_for_logos_to_open()

    ou_dedetai.run(["--stop-installed-app"])


def test_install() -> OuDedetai:
    ou_dedetai = OuDedetai(log_level="debug")
    ou_dedetai.uninstall()
    ou_dedetai.run(["--install-app", "--assume-yes"])
    return ou_dedetai


class WineDBGRunning(Exception):
    """Exception to keep track of when we noticed winedbg is running
    
    Useful as an exeception as it can be caught and ignored if desired."""


def raise_if_winedbg_is_running():
    """Raises exception if winedbg was found to be running"""

    if 'winedbg' in [proc.name() for proc in psutil.process_iter(['name'])]:
        raise WineDBGRunning


def pre_input_tasks():
    """It's possible sending keystrokes could close dialogs
    
    We want to check a couple things before sending new a keypress"""
    raise_if_winedbg_is_running()


def type_string(string: str):
    """Types string
    
    Uses xdotool on Xorg"""
    pre_input_tasks()
    # FIXME: not sure if we can do this in wayland
    if not os.getenv("DISPLAY"):
        raise Exception("This test only works under Xorg")
    run_cmd(["xdotool", "type", string])

def press_keys(keys: str | list[str]):
    """Presses key
    
    Uses xdotool on Xorg"""
    pre_input_tasks()
    if isinstance(keys, str):
        keys = [keys]
    # FIXME: not sure if we can do this in wayland
    if not os.getenv("DISPLAY"):
        raise Exception("This test only works under Xorg")
    for key in keys:
        run_cmd(["xdotool", "key", key])
        time.sleep(.5)


def main():
    # FIXME: also test the beta channel of Logos?

    # FIXME: consider loop to run all of these in their supported distroboxes (https://distrobox.it/)
    # ou_dedetai = test_install()
    ou_dedetai = OuDedetai(log_level="debug", isolate=True)
    ou_dedetai.run(["--install-app", "--assume-yes"])

    ou_dedetai.start_app()

    # XXX: move this to a function and also have two options - one login first time resources, the other restoring from a backup (for speed)
    # If we were given credentials, use them to login and download resources.
    # This may take some time.
    # Useful bash script to set these
    """
    export LOGOS_USERNAME=`read -p "Username: " foo;echo $foo`;
    export LOGOS_PASSWORD=`read -p "Password: " -s foo;echo $foo`;
    echo
    """
    logos_username = os.getenv("LOGOS_USERNAME")
    logos_password = os.getenv("LOGOS_PASSWORD")
    # These key sequences were tested on Logos 41.
    # If the installer changes form, we'll need to adjust this.
    if logos_username and logos_password:
        # Wait for the Logos UI to display
        time.sleep(10)
        # Now test to see if we can login.
        # This test is designed to take some time
        # Prefer more robust times over quicker tests.
        type_string(logos_username)
        press_keys("Tab")
        type_string(logos_password)
        press_keys("Return")
        # Time delay... This may be variable, but we have no way to check
        # Took 10 seconds on my machine, double for safety.
        time.sleep(20)

        # XXX: found a crash here if you go back during a specific time early on


        # Three tabs and a space agrees with second option (essential/minimal). 
        # Some accounts with very little resources do not have 3 options, but 2.
        press_keys(["Tab", "Tab", "Tab", "Tab", "space"])
        # Then shift+Tab three times to get to the continue button.
        # We need to use shift tab, as some accounts have three options in the radio
        # (Full/essential/minimal), others only have (full/minimal)
        # so we can't count on how many tabs to go down
        press_keys(["Shift+Tab", "Shift+Tab", "Shift+Tab", "Return"])
        # Wait for the UI to settle - we can wait here longer than we need to
        time.sleep(30)
        # Hit Continue again
        press_keys(["Tab", "Return"])
        # Now we wait for resources to download. Extremely variable.
        # The continue button isn't tab navigable at this point in the install
        #
        # Wait until no files have been touched for a minute
        # Then stop and restart logos. This should unstuck any stuck state.
        # For example when testing this my download got stuck at 66%
        # But stopping and restarting fixed.

        # XXX: should we enforce this is true? (AKA always isolated?)
        assert ou_dedetai.install_dir
        logos_appdata_dir = None
        for file in Path(ou_dedetai.install_dir).glob("data/wine64_bottle/drive_c/users/*/AppData/Local/Logos"): #noqa: E501
            logos_appdata_dir = str(file)
            break
        assert logos_appdata_dir

        wait_for_directory_to_be_untouched(logos_appdata_dir, 60)

        ou_dedetai.stop_app()
        ou_dedetai.start_app()

        # Now wait for it to start (30 seconds is arbitrary)
        time.sleep(30)

        def run_command_box(command: str):
            press_keys(["Escape", "alt+c"])
            time.sleep(2)
            type_string(command)
            time.sleep(8)
            press_keys("Return")

        def open_guide(guide: str):
            press_keys(["Escape", "alt+g"])
            time.sleep(2)
            type_string(guide)
            time.sleep(3)
            press_keys(["Tab", "Return"])
            time.sleep(5)

        def open_tool(guide: str):
            press_keys(["Escape", "alt+t"])
            time.sleep(2)
            type_string(guide)
            time.sleep(10)
            press_keys(["Tab", "Tab", "Return"])

        # Now try to do some things

        # Open John 3.16 (probably in a layout of some kind)
        # Command box results are variable
        run_command_box("John 3:16")
        # Let it settle
        time.sleep(10)
        run_command_box("Jesus factbook")

        # XXX: move this out to a negative test, and switch it from a delte to a move so we can put it back.
        # XXX: Sabotage! For the sake of a crash. Still needs testing to force a crash. 
        # We may want to have a negative test for this to ensure our logic to detect
        # logos idn't crash still functions later on in time.
        # This may or may not work as the data might already be loaded.
        shutil.rmtree(f"{ou_dedetai.install_dir}/data/wine64_bottle/drive_c/windows/Fonts/")

        open_guide("bible word study")
        time.sleep(5)
        type_string("worship")
        press_keys("Return")
        # Let it settle
        time.sleep(4)

        open_tool("copy bible verses")

        # Now ensure the Logos window is still open
        # AND there is no winedbg process running (this is checked on input too)
        try:
            wait_for_logos_to_open()
            raise_if_winedbg_is_running()
        except Exception:
            print("Test failed")
            raise

        print("Logos didn't crash while testing")
        ou_dedetai.stop_app()
        # Pass!

    ou_dedetai.uninstall()


    # Untested:
    # - run_indexing - Need to be logged in
    # - edit-config - would need to modify EDITOR for this, not a lot of value
    # --install-dependencies - would be easy enough to run this, but not a real test
    #   considering the machine the tests are running on probably already has it
    #   installed and it's already run in install-all
    # --update-self - we might be able to fake it into thinking we're an older version
    # --update-latest-appimage - we're already at latest as a result of install-app
    # --install-* - already effectively tested as a result of install-app, may be 
    #   difficult to confirm independently
    # --set-appimage - just needs to be implemented
    # --get-winetricks - no need to test independently, covered in install_app
    # --run-winetricks - needs a way to cleanup after this spawns
    # --toggle-app-logging - difficult to confirm
    # --create-shortcuts - easy enough, unsure the use of this, shouldn't this already
    #   be done? Nothing in here should change? The user can always re-run the entire
    #   process if they want to do this
    # --winetricks - unsure how'd we confirm it work

    # Final message
    print("Tests passed.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
