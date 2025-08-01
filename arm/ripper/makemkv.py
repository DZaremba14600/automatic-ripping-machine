#!/usr/bin/env python3
"""
Main file for dealing with connecting to MakeMKV and handling errors

Reference:
- https://www.makemkv.com/developers/usage.txt
- https://github.com/automatic-ripping-machine/automatic-ripping-machine/wiki/MakeMKV-Codes
"""

import collections
import dataclasses
import enum
import itertools
import logging
import os
import subprocess

import shlex
import shutil
from time import sleep

from arm.models import Track, SystemDrives
from arm.models.job import JobState
from arm.ripper import utils
from arm.ui import db
import arm.config.config as cfg

from arm.ripper.utils import notify


MAKEMKV_INFO_WAIT_TIME = 60  # [s]
"""Wait for concurrent MakeMKV info processes.
This is introduced due to a race condition creating makemkvcon zombies
"""
MAKEMKV_UNKNOWN_DRV = 999
"""Currently the value is always 999"""

MAKEMKV_STREAM_CODE_TYPE_VIDEO = 6201
"""Identifies the stream as a Video stream"""
MAX_DEVICES = 16
"""MakeMKV Optical Devices Limit"""
SOURCE = "MakeMKV"
"""Used as input argument for put_track"""

ERROR_MESSAGE_OPERATION_RESULT = "Internal error - Operation result is incorrect (132)"
ERROR_MESSAGE_TRAY_OPEN = "Scsi error - NOT READY:MEDIUM NOT PRESENT - TRAY OPEN"
ERROR_MESSAGE_MEDIUM_ERROR = "Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR"
ERROR_MESSAGE_HARDWARE_ERROR = "Scsi error - HARDWARE ERROR:441E"


class OutputType(enum.Flag):
    """
    MakeMKV Output Types

    The Output Type are the first characters in a stdout line generated by
    makemkvcon before the colon.
    """
    DRV = enum.auto()
    """Drive"""
    MSG = enum.auto()
    """Message"""
    CINFO = enum.auto()
    """Disc Info"""
    SINFO = enum.auto()
    """Stream Info"""
    TCOUNT = enum.auto()
    """Title Count"""
    TINFO = enum.auto()
    """Title Info"""
    PRGV = enum.auto()
    """Progress Bar Value"""
    PRGC = enum.auto()
    """Progress Bar Current Progress on Title"""
    PRGT = enum.auto()
    """Progress Bar Total Progress on Title"""


class DriveVisible(enum.IntEnum):
    """
    Definitions of `DriveInformation.visible` colon of the OutputType.DRV.
    """
    EMPTY = 0
    OPEN = 1
    LOADED = 2
    LOADING = 3
    NOT_ATTACHED = 256

    @classmethod
    def _missing_(cls, value):
        logging.debug(f"Undefined Visible Value {value} in {cls}")
        return cls.NOT_ATTACHED


class DriveType(enum.Enum):
    """
    Definitions of the Drive Type issued by OutputType.DRV
    """
    CD = 0
    DVD = 1
    BD_TYPE1 = 12
    BD_TYPE2 = 28
    """Both 12 and 28 are blu ray drives.
    Note: Not sure what the difference is.
    """

    @classmethod
    def _missing_(cls, value):
        logging.debug(f"Undefined Drive Type {value} in {cls}")
        return cls.CD


class MessageID(enum.IntEnum):
    """
    Known MakeMKV Message Codes
    """

    LIBMKV_TRACE = 1002
    """LIBMKV_TRACE: %1
    - "Exception: Error in p->FetchFrames(1,false)"
    """
    VERSION_INFO = 1005
    """%1 started"""
    GENERIC_INFO = 1011
    """%1"""
    READ_ERROR = 2003
    """Error '%1' occurred while reading '%2' at offset '%3'
    Indicates (mostly non-fatal) Read Error
    """
    WRITE_ERROR = 2019
    """Error '%1' occurred while creating '%2'
    Indicates (mostly fatal) Write Error
    """
    COMPLEX_MULTIPLEX = 3024
    """Complex multiplex encountered
    Usually takes more time to process.
    """
    TITLE_SKIPPED = 3025
    TITLE_ADDED = 3028
    AUDIO_SKIPPED_EMPTY = 3034
    """Audio stream #%1 in title #%2 looks empty and was skipped"""
    SUBTITLE_SKIPPED_IDENTICAL = 3030
    """Subtitle stream #%1 is identical to stream #%2 and was skipped"""
    FILE_ADDED = 3307
    """File %1 was added as title #%2"""
    RIP_TITLE_ERROR = 5003
    """Failed to save title %1 to file %2"""
    RIP_COMPLETED = 5004
    """%1 titles saved, %2 failed"""
    RIP_DISC_OPEN_ERROR = 5010
    """Failed to open disc (mostly fatal)"""
    RIP_SUMMARY_BEFORE = 5014
    """Saving %1 titles into directory %2"""
    RIP_SUMMARY_AFTER = 5037
    """Copy complete. %1 titles saved, %2 failed."""
    EVALUATION_PERIOD_EXPIRED_INFO = 5052
    """Evaluation period has expired.
    Please purchase an activation key if you've found this application useful.
    You may still use all free functionality without any restrictions.
    """
    EVALUATION_PERIOD_EXPIRED_SHAREWARE = 5055
    """Evaluation period has expired, shareware functionality unavailable."""
    RIP_BACKUP_FAILED_PRE = 5096
    RIP_BACKUP_FAILED = 5080
    """Backup Mode Failed."""


class StreamID(enum.IntEnum):
    """
    Definition of the Stream ID and its reference to the stored content
    """
    UNKNOWN = 0
    TYPE = 1
    ASPECT = 20
    FPS = 21


class TrackID(enum.IntEnum):
    """
    Definition of the Track ID and its reference to the stored content
    """
    DURATION = 9
    FILENAME = 27


@dataclasses.dataclass
class MakeMKVMessage:
    """
    Message output

    `MSG:code,flags,count,message,format,param0,param1,...`
    """
    code: int
    """Unique Message Code"""
    flags: int
    """Message Flags"""
    count: int
    """Number of Parameters"""
    message: str
    """Formatted Message"""
    sprintf: str
    """Unformatted Message"""

    def __post_init__(self):
        self.code = int(self.code)
        self.flags = int(self.flags)
        self.count = int(self.count)


@dataclasses.dataclass
class MakeMKVErrorMessage(MakeMKVMessage):
    """Error Message"""
    error: str

    def __post_init__(self):
        if len(self.sprintf) < 2:
            raise ValueError(self.sprintf)
        self.error = str(self.sprintf[1])
        self.sprintf = self.sprintf[2:]


@dataclasses.dataclass
class Titles:
    """
    Disc information output messages

    `TCOUT:count`
    """
    count: int
    """Titles Count"""

    def __post_init__(self):
        self.count = int(self.count)


@dataclasses.dataclass
class CInfo:
    """
    Disc Information

    `CINFO:id,code,value`
    ```
    """
    id: int  # pylint: disable=C0103
    """Attribute ID, see AP_ItemAttributeId in apdefs.h"""
    code: int
    """Message Code if Attribute Value is a constant string"""
    value: str
    """Attribute Value"""

    def __post_init__(self):
        self.id = int(self.id)
        self.code = int(self.code)


@dataclasses.dataclass
class TInfo(CInfo):
    """
    Title Information

    `TINFO:tid,id,code,value`
    """
    tid: int
    """Title ID"""

    def __post_init__(self):
        super().__post_init__()
        self.tid = int(self.tid)


@dataclasses.dataclass
class SInfo(TInfo):
    """
    Stream Information

    `SINFO:id,code,value`

    """
    sid: int

    def __post_init__(self):
        super().__post_init__()
        self.sid = int(self.sid)


@dataclasses.dataclass
class ProgressBarValues:
    """
    Progress bar values for current and total progress

    PRGV:current,total,max
    """
    current: int
    """current progress value"""
    total: int
    """total progress value"""
    maximum: int
    """maximum possible value for a progress bar, constant"""

    def __post_init__(self):
        self.current = int(self.current)
        self.total = int(self.total)
        self.maximum = int(self.maximum)


@dataclasses.dataclass
class ProgressBarTitle:
    """
    Progress Bar Information
    """
    code: int
    """unique message code"""
    oid: int
    """operation sub-id"""
    name: str
    """name string"""

    def __post_init__(self):
        self.code = int(self.code)
        self.oid = int(self.oid)


@dataclasses.dataclass
class ProgressBarCurrent(ProgressBarTitle):
    """
    Current progress title

    PRGC:code,id,name
    """


@dataclasses.dataclass
class ProgressBarTotal(ProgressBarTitle):
    """
    Total progress title

    PRGT:code,id,name
    """


@dataclasses.dataclass(order=True)
class DriveInformation:
    """
    Basic Optical Drive Information from MakeMKV Drive Scan Messages

    `DRV:index,visible,enabled,flags,drive name,disc name`

    @see arm.ui.settings.DriveUtils.Drive
    """

    mount: str
    """Device Name (sort index, dynamic)"""
    disc: str
    """Media title/label (changes with disc)"""
    info: str
    """Drive Name (changes on FW update)"""
    flags: int
    """Disc Type (persistent)"""
    enabled: bool
    """Unknown Purpose, always True"""
    visible: bool
    """Drive Present"""
    index: int
    """MakeMKV disc index"""

    def __post_init__(self):
        self.flags = int(self.flags)
        self.enabled = bool(int(self.enabled) == MAKEMKV_UNKNOWN_DRV)
        self.visible = int(self.visible)
        self.index = int(self.index)


@dataclasses.dataclass
class Drive(DriveInformation):
    """
    Extended MakeMKV Drive Information (with medium information)
    """
    loaded: bool = dataclasses.field(init=False, default=False)
    """Device has Medium loaded (changes)"""
    open: bool = dataclasses.field(init=False, default=False)
    """Device Tray is open"""
    attached: bool = dataclasses.field(init=False, default=True)
    """Device is attached / available for the system"""
    media_cd: bool = dataclasses.field(init=False, default=False)
    """Medium is CD"""
    media_dvd: bool = dataclasses.field(init=False, default=False)
    """Medium is DVD"""
    media_bd: bool = dataclasses.field(init=False, default=False)
    """Medium is BD"""

    def __post_init__(self):
        super().__post_init__()
        drive_type = DriveType(self.flags)
        if drive_type == DriveType.CD:
            self.media_cd = True
        elif drive_type == DriveType.DVD:
            self.media_dvd = True
        elif drive_type == DriveType.BD_TYPE1:
            self.media_bd = True
        elif drive_type == DriveType.BD_TYPE2:
            self.media_bd = True
        drive_visible = DriveVisible(self.visible)
        if drive_visible == DriveVisible.EMPTY:
            self.loaded = False
        elif drive_visible == DriveVisible.OPEN:
            self.open = True
        elif drive_visible == DriveVisible.LOADED:
            self.loaded = True
        elif drive_visible == DriveVisible.LOADING:
            self.loaded = True
        elif drive_visible == DriveVisible.NOT_ATTACHED:
            self.attached = False


class MakeMkvParserError(ValueError):
    """Exception raised when the stdout line of makemkvcon cannot get parsed."""


class MakeMkvRuntimeError(RuntimeError):
    """
    Exception raised when a CalledProcessError is thrown during execution of a
    `makemkvcon` command.

    Attributes:
        message: the explanation of the error
    """

    def __init__(self, returncode, cmd, output=None, stderr=None):
        logging.debug(f"MakeMKV command: '{' '.join(cmd)}'")
        if output is not None:
            logging.debug(f"MakeMKV output: {output}")
        if stderr is not None:
            logging.debug(f"MakeMKV stderr: {stderr}")
        self.message = f"Call to MakeMKV failed with code: {returncode}"
        logging.error(self.message)
        super().__init__(self.message)


def parse_content(content, num_header, num_message):
    """
    Helper Function to parse the MakeMKV Messages

    >>> msg = '1005,0,1,"MakeMKV v1.17.8 linux(x64-release) started","%1 started","MakeMKV v1.17.8 linux(x64-release)"'
    >>> list(parse_content(msg, 3, -1))  # MSG
    ['1005', '0', '1', 'MakeMKV v1.17.8 linux(x64-release) started', '%1 started', 'MakeMKV v1.17.8 linux(x64-release)']
    >>> msg = '0'
    >>> list(parse_content(msg, 0, 0))  # TCOUT
    ['0']
    >>> msg = '6,256,999,0,"BD-Drive","THE TITLE","/dev/sr0"'
    >>> list(parse_content(msg, 4, 2))  # DRV
    ['6', '256', '999', '0', 'BD-Drive', 'THE TITLE', '/dev/sr0']
    >>> msg = '1,6209,"Blu-ray disc"'
    >>> list(parse_content(msg, 2, 0))  # CINFO
    ['1', '6209', 'Blu-ray disc']
    >>> msg = '1,26,0,"155,156,157"'
    >>> list(parse_content(msg, 3, 0))  # TINFO
    ['1', '26', '0', '155,156,157']
    >>> msg = '0,0,28,0,"ger"'
    >>> list(parse_content(msg, 4, 0))  # SINFO
    ['0', '0', '28', '0', 'ger']
    """
    # The header is considered as the first n non-string entries
    header = content.split(",", maxsplit=num_header)
    # (str) messages wrapped in double quotes *may* contain comma
    message = header[-1].split('","', maxsplit=num_message)
    return itertools.chain(header[:-1], (x.strip('"') for x in message))


def parse_line(line):
    """Parse MakeMkv Output Line to DataClasses"""
    if ":" not in line:
        raise MakeMkvParserError("No Message Type Detected")
    msg_type, content = line.split(":", maxsplit=1)
    if msg_type not in OutputType.__members__:
        raise MakeMkvParserError(f"Cannot parse '{msg_type}':'{content}'")
    msg_type = OutputType[msg_type]
    if msg_type == OutputType.MSG:
        temp = parse_content(content, 3, -1)
        data = MakeMKVMessage(*itertools.islice(temp, 4), list(temp))
        message = MakeMKVOutputChecker(data).check()
    elif msg_type == OutputType.PRGV:
        message = ProgressBarValues(*parse_content(content, 2, 0))
    elif msg_type == OutputType.PRGC:
        message = ProgressBarCurrent(*parse_content(content, 2, 0))
    elif msg_type == OutputType.PRGT:
        message = ProgressBarTotal(*parse_content(content, 2, 0))
    elif msg_type == OutputType.SINFO:
        tid, sid, *info = parse_content(content, 4, 0)
        message = SInfo(*info, tid, sid)
    elif msg_type == OutputType.TINFO:
        tid, *info = parse_content(content, 3, 0)
        message = TInfo(*info, tid)
    elif msg_type == OutputType.CINFO:
        message = CInfo(*parse_content(content, 2, 0))
    elif msg_type == OutputType.DRV:
        message = Drive(*reversed(list(parse_content(content, 4, 2))))
    elif msg_type == OutputType.TCOUNT:
        message = Titles(*parse_content(content, 0, 0))
    else:
        raise MakeMkvParserError(f"Cannot handle '{msg_type}':'{content}'")
    return msg_type, message


def makemkv_info(job, select=None, index=9999, options=None):
    """
    Use MakeMKV info to search the system for optical drives

    Parameters:
        job: arm.models.job.Job
        select (OutputType): Message Type (default: all)
        index: Makemkv disc index (default: all)
        options: Additional options to be passed to makemkvcon (default: [])
    Yields:
        dataclasses of selected type:
            - Message
            - Titles
            - CInfo
            - TInfo
            - SInfo

    ```
    $ makemkvcon -r info disc:9999
    MSG:1005,0,1,"MakeMKV v1.17.8 linux(x64-release) started","%1 started","MakeMKV v1.17.8 linux(x64-release)"
    DRV:0,1,999,0,"BD-RE PIONEER BD-RW   BDR-UD04 1.14 BCDL000001WL","","/dev/sr2"
    DRV:1,1,999,0,"BD-RE PIONEER BD-RW   BDR-UD04 1.14 BCDL000002WL","","/dev/sr0"
    DRV:2,1,999,0,"BD-RE PIONEER BD-RW   BDR-UD04 1.14 BCDL000003WL","","/dev/sr3"
    DRV:3,1,999,0,"BD-RE PIONEER BD-RW   BDR-UD04 1.14 BCDL000004WL","","/dev/sr5"
    DRV:4,1,999,0,"BD-RE PIONEER BD-RW   BDR-UD04 1.14 BCDL000005WL","","/dev/sr4"
    DRV:5,1,999,0,"BD-RE PIONEER BD-RW   BDR-UD04 1.14 BCDL000006WL","","/dev/sr1"
    DRV:6,256,999,0,"","",""
    DRV:7,256,999,0,"","",""
    DRV:8,256,999,0,"","",""
    DRV:9,256,999,0,"","",""
    DRV:10,256,999,0,"","",""
    DRV:11,256,999,0,"","",""
    DRV:12,256,999,0,"","",""
    DRV:13,256,999,0,"","",""
    DRV:14,256,999,0,"","",""
    DRV:15,256,999,0,"","",""
    MSG:5010,0,0,"Failed to open disc","Failed to open disc"
    TCOUNT:0
    ```
    """
    if select is None:
        select = OutputType.MSG | OutputType.TCOUNT | OutputType.DRV
    if options is None:
        options = []
    if not isinstance(options, list):
        raise TypeError(options)
    # 1MB cache size to get info on the specified disc(s)
    info_options = ["info", "--cache=1"] + options + [f"disc:{index:d}"]
    wait_time = job.config.MANUAL_WAIT_TIME
    max_processes = job.config.MAX_CONCURRENT_MAKEMKVINFO
    job.status = JobState.VIDEO_WAITING.value
    db.session.commit()
    utils.sleep_check_process("makemkvcon", max_processes, sleep=(10, wait_time, 10))
    job.status = JobState.VIDEO_INFO.value
    db.session.commit()
    try:
        yield from run(info_options, select)
    finally:
        logging.info("MakeMKV info exits.")
        job.status = JobState.VIDEO_WAITING.value
        db.session.commit()
        if max_processes:
            logging.info(f"Penalty {wait_time}s")
            # makemkvcon info tends to crash makemkvcon backup|mkv
            # give other processes time to use this function.
            sleep(wait_time)
        # sleep here until all processes finish (hopefully)
        utils.sleep_check_process("makemkvcon", max_processes, sleep=wait_time)
        job.status = JobState.VIDEO_RIPPING.value
        db.session.commit()


def get_drives(job):
    """Get information for all active optical drives

    Parameters:
        job: arm.models.job.Job
    """
    for drive in makemkv_info(job, select=OutputType.DRV):
        if drive.attached:
            yield drive


def makemkv_backup(job, rawpath):
    """
    Rip BluRay with Backup Method

    Parameters:
        job: arm.models.job.Job
        rawpath:
    """
    # backup method
    cmd = [
        "backup",
        "--decrypt",
    ]
    cmd += shlex.split(job.config.MKV_ARGS)
    cmd += [
        f"--minlength={job.config.MINLENGTH}",
        f"--progress={progress_log(job)}",
        f"disc:{job.drive.mdisc:d}",
        rawpath,
    ]
    logging.info("Backing up disc")
    collections.deque(run(cmd, OutputType.MSG), maxlen=0)


def makemkv_mkv(job, rawpath):
    """
    Rip Blu-ray without enhanced protection or dvd disc

    Parameters:
        job: arm.models.job.Job
        rawpath:
    """
    # Get drive mode for the current drive
    mode = utils.get_drive_mode(job.devpath)
    logging.info(f"Job running in {mode} mode")
    # Get track info form mkv rip
    get_track_info(job.drive.mdisc, job)
    # route to ripping functions.
    if job.config.MAINFEATURE:
        logging.info("Trying to find mainfeature")
        track = Track.query.filter_by(job_id=job.job_id).order_by(Track.length.desc()).first()
        rip_mainfeature(job, track, rawpath)
    elif mode == 'manual':  # Run if mode is manual, user selects tracks
        # Set job status to waiting
        job.status = JobState.VIDEO_WAITING.value
        db.session.commit()
        # Process Tracks
        if manual_wait(job):  # Alert user: tracks are ready and wait for 30 minutes
            # Response from user provided, process requested tracks
            job.status = JobState.VIDEO_RIPPING.value
            db.session.commit()
            process_single_tracks(job, rawpath, mode)
        else:
            # Notify User: no action was taken
            title = "ARM is Sad - Job Abandoned"
            message = "You left me alone in the cold and dark, I forgot who I was. Your job has been abandoned."
            notify(job, title, message)

            # Setting rawpath to None to set the job as failed when returning to arm_ripper
            rawpath = None
    # if no maximum length, process the whole disc in one command
    elif int(job.config.MAXLENGTH) > 99998:
        cmd = [
            "mkv",
        ]
        cmd += shlex.split(job.config.MKV_ARGS)
        cmd += [
            f"--progress={progress_log(job)}",
            f"dev:{job.devpath}",
            "all",
            rawpath,
            f"--minlength={job.config.MINLENGTH}",
        ]
        logging.info("Process all tracks from disc.")
        collections.deque(run(cmd, OutputType.MSG), maxlen=0)
    else:
        process_single_tracks(job, rawpath, 'auto')


def makemkv(job):
    """
    Rip Blu-rays/DVDs with MakeMKV

    Parameters:
        job: arm.models.job.Job
    Returns:
        str: path to ripped files.
    """
    # confirm MKV is working, beta key hasn't expired
    prep_mkv()
    logging.info(f"Starting MakeMKV rip. Method is {job.config.RIPMETHOD}")
    # get MakeMKV disc number
    if job.drive.mdisc is None:
        logging.debug("Storing new MakeMKV disc numbers to database.")
        with db.session.no_autoflush:
            for drive in get_drives(job):
                for db_drive in SystemDrives.query.filter_by(mount=drive.mount).all():
                    db_drive.mdisc = drive.index
                    db.session.add(db_drive)
        db.session.commit()
    logging.info(f"MakeMKV disc number: {job.drive.mdisc:d}")
    # get filesystem in order
    rawpath = setup_rawpath(job, os.path.join(str(job.config.RAW_PATH), str(job.title)))
    logging.info(f"Processing files to: {rawpath}")
    # Rip BluRay
    if (job.config.RIPMETHOD in ("backup", "backup_dvd")) and job.disctype == "bluray":
        makemkv_backup(job, rawpath)
    # Rip BluRay or DVD
    elif job.config.RIPMETHOD == "mkv" or job.disctype == "dvd":
        makemkv_mkv(job, rawpath)
    else:
        logging.info("I'm confused what to do....  Passing on MakeMKV")
    job.eject()
    logging.info(f"Exiting MakeMKV processing with return value of: {rawpath}")
    return rawpath


def rip_mainfeature(job, track, rawpath):
    """
    Find and rip only the main feature when using Blu-rays

    Parameters:
        job: arm.models.job.Job
        track: arm.models.track.Track
    """
    logging.info("Processing track#{num} as mainfeature. Length is {seconds}s",
                 num=track.track_number, seconds=track.length)
    filepathname = os.path.join(rawpath, track.filename)
    logging.info(f"Ripping track#{track.track_number} to {shlex.quote(filepathname)}")
    cmd = [
        "mkv",
    ]
    cmd += shlex.split(job.config.MKV_ARGS)
    cmd += [
        f"--progress={progress_log(job)}",
        f"dev:{job.devpath}",
        track.track_number,
        rawpath,
        f"--minlength={job.config.MINLENGTH}",
    ]
    logging.info("Ripping main feature")
    # Possibly update db to say track was ripped
    collections.deque(run(cmd, OutputType.MSG), maxlen=0)


def process_single_tracks(job, rawpath, mode: str):
    """
    Process single tracks by MakeMKV one at a time

    Parameters:
        job: arm.models.job.Job
        rawpath:
        mode: drive mode (auto or manual)
    """
    # process one track at a time based on track length
    for track in job.tracks:
        # Process single track automatically based on start and finish times
        if mode == 'auto':
            if track.length < int(job.config.MINLENGTH):
                # too short
                logging.info(f"Track #{track.track_number} of {job.no_of_titles}. Length ({track.length}) "
                             f"is less than minimum length ({job.config.MINLENGTH}).  Skipping")
                track.process = False

            elif track.length > int(job.config.MAXLENGTH):
                # too long
                logging.info(f"Track #{track.track_number} of {job.no_of_titles}. "
                             f"Length ({track.length}) is greater than maximum length ({job.config.MAXLENGTH}).  "
                             "Skipping")
                track.process = False
            else:
                # track is just right
                track.process = True

        # Rip the track if the user has set it to rip, or in auto mode and the time is good
        if track.process:
            logging.info(f"Processing track #{track.track_number} of {(job.no_of_titles - 1)}. "
                         f"Length is {track.length} seconds.")
            filepathname = os.path.join(rawpath, track.filename)
            logging.info(f"Ripping title {track.track_number} to {shlex.quote(filepathname)}")

            cmd = [
                "mkv",
            ]
            cmd += shlex.split(job.config.MKV_ARGS)
            cmd += [
                f"--progress={progress_log(job)}",
                f"dev:{job.devpath}",
                track.track_number,
                rawpath,
            ]
            logging.debug("Starting to rip single track.")
            collections.deque(run(cmd, OutputType.MSG), maxlen=0)


def setup_rawpath(job, raw_path):
    """
    Checks if we need to create path and does so if needed\n\n

    Parameters:
        job: arm.models.job.Job
        raw_path
    Returns:
        str: modified path
    """

    logging.info(f"Destination is {raw_path}")
    if not os.path.exists(raw_path):
        try:
            os.makedirs(raw_path)
        except OSError:
            err = f"Couldn't create the base file path: {raw_path}. Probably a permissions error"
            logging.error(err)
    else:
        logging.info(f"{raw_path} exists.  Adding timestamp.")
        raw_path = os.path.join(str(job.config.RAW_PATH), f"{job.title}_{job.stage}")
        logging.info(f"raw_path is {raw_path}")
        try:
            os.makedirs(raw_path)
        except OSError:
            err = f"Couldn't create the base file path: {raw_path}. Probably a permissions error"
            raise OSError(err) from OSError
    return raw_path


def prep_mkv():
    """
    Make sure the MakeMKV key is up-to-date

    Raises:
        MakeMkvRuntimeError
    """
    try:
        logging.info("Updating MakeMKV key...")
        cmd = [
            "/bin/bash",
            "/opt/arm/scripts/update_key.sh",
        ]
        # if MAKEMKV_PERMA_KEY is populated
        if cfg.arm_config['MAKEMKV_PERMA_KEY'] is not None and cfg.arm_config['MAKEMKV_PERMA_KEY'] != "":
            logging.debug("MAKEMKV_PERMA_KEY populated, using that...")
            # add MAKEMKV_PERMA_KEY as an argument to the command
            cmd += [cfg.arm_config['MAKEMKV_PERMA_KEY']]
        proc = subprocess.run(cmd, capture_output=True, shell=True, check=True)
        logging.debug(proc.stdout)
    except subprocess.CalledProcessError as err:
        logging.debug(err.stdout)
        logging.error(f"Error updating MakeMKV key, return code: {err.returncode}")
        raise MakeMkvRuntimeError(err.returncode, cmd, output=err.stdout) from err


def progress_log(job):
    """
    Retrieve the path to the progress log file

    The file path is expected like this by the frontend.

    ToDo: move to Job() class since this is mainly a db wrapper. Then allow the
          front end to pick up the same method.

    Parameters:
        job: arm.models.job.Job
    Returns:
        str: log file

    """
    logfile = os.path.join(job.config.LOGPATH, "progress", f"{job.job_id:d}.log")
    logging.debug(f"logging progress to '{logfile}'")
    return shlex.quote(logfile)


class TrackInfoProcessor:
    """
    Processes MakeMKV track info messages to update Track class.
    """

    def __init__(self, job, index):
        self.job = job
        self.index = index

        # Initialize track-related state variables
        self.track_id = None
        self.seconds = 0
        self.aspect = ""
        self.fps = 0.0
        self.filename = ""
        self.stream_type = None

    def process_messages(self):
        output_types = (
            OutputType.CINFO |
            OutputType.SINFO |
            OutputType.TCOUNT |
            OutputType.TINFO
        )
        options = []  # add relevant options here if needed

        for message in makemkv_info(self.job, select=output_types, index=self.index, options=options):
            self._process_message(message)

        # Add the last track if exists
        self._add_track()

    def _process_message(self, message):
        if isinstance(message, (TInfo, SInfo)):
            self._handle_track_or_stream_info(message)
        elif isinstance(message, Titles):
            self._handle_titles(message)

    def _handle_track_or_stream_info(self, message):
        # Detect new track, add previous one if changed
        if self.track_id is not None and message.tid != self.track_id:
            self._add_track()
        self.track_id = message.tid

        if isinstance(message, SInfo):
            assert message.tid == self.track_id, message
            self._handle_sinfo(message)
        elif isinstance(message, TInfo):
            assert message.tid == self.track_id, message
            self._handle_tinfo(message)

    def _handle_sinfo(self, message):
        if message.id == StreamID.TYPE:
            self.stream_type = message.code
        elif self.stream_type == MAKEMKV_STREAM_CODE_TYPE_VIDEO:
            if message.id == StreamID.ASPECT:
                self.aspect = message.value.strip()
            elif message.id == StreamID.FPS:
                self.fps = float(message.value.split()[0])

    def _handle_tinfo(self, message):
        if message.id == TrackID.FILENAME:
            # Extract filename between quotes
            self.filename = next(iter(message.value.split('"')[1::2]), message.value)
        elif message.id == TrackID.DURATION:
            self.seconds = convert_to_seconds(message.value.strip())

    def _handle_titles(self, message):
        logging.info(f"Found {message.count:d} titles")
        utils.database_updater({"no_of_titles": message.count}, self.job)

    def _add_track(self):
        if self.track_id is None:
            return
        utils.put_track(
            self.job,
            self.track_id,
            self.seconds,
            self.aspect,
            str(self.fps),
            False,
            SOURCE,
            self.filename
        )
        # Reset track info after adding if needed
        self.seconds = 0
        self.aspect = ""
        self.fps = 0.0
        self.filename = ""


def get_track_info(index, job):
    """
    Use MakeMKV to get track info and update Track class

    Parameters:
        index: Makemkv disc index
        job: arm.models.job.Job
    Returns:
        None

    .. note:: For help with MakeMKV codes:
    https://github.com/automatic-ripping-machine/automatic-ripping-machine/wiki/MakeMKV-Codes
    """
    processor = TrackInfoProcessor(job, index)
    processor.process_messages()


def convert_to_seconds(hms_value):
    """
    Find the title length in track info MakeMKV message

    Parameters:
        hms_value: Time in format H:MM:SS
    Returns:
        int: Time in seconds
    """
    hour, mins, secs = hms_value.split(":")
    return int(hour) * 3600 + int(mins) * 60 + int(secs)


class MakeMKVOutputChecker:
    """
    Check MakeMKV output messages for errors and handle special cases.

    Parameters:
        data (MakeMKVMessage): the message to check.
    """

    READ_ERROR_MAP = {
        ERROR_MESSAGE_OPERATION_RESULT: (logging.critical, 'error possibly fatal, creating zombie processes'),
        ERROR_MESSAGE_TRAY_OPEN: (logging.info, 'error mostly non fatal'),
        ERROR_MESSAGE_MEDIUM_ERROR: (logging.critical, 'error possibly fatal, medium removed during mkv backup'),
        ERROR_MESSAGE_HARDWARE_ERROR: (logging.critical, 'error possibly fatal, medium removed during mkv backup'),
    }

    LOG_ONLY_CODES = {
        MessageID.RIP_DISC_OPEN_ERROR: logging.info,
        MessageID.RIP_TITLE_ERROR: logging.warning,
        MessageID.RIP_COMPLETED: logging.info,
        MessageID.LIBMKV_TRACE: logging.warning,
        MessageID.RIP_BACKUP_FAILED_PRE: logging.warning,
        MessageID.EVALUATION_PERIOD_EXPIRED_INFO: logging.warning,
    }

    SPECIAL_ERROR_CODES = {
        MessageID.EVALUATION_PERIOD_EXPIRED_SHAREWARE,
        MessageID.RIP_BACKUP_FAILED,
    }

    def __init__(self, data: MakeMKVMessage):
        if not isinstance(data, MakeMKVMessage):
            raise TypeError(f"Expected MakeMKVMessage, got {type(data)}")
        self.data = data

    def check(self):
        """Dispatch processing based on message code."""
        code = self.data.code

        if code == MessageID.READ_ERROR:
            return self.read_error()

        if code == MessageID.WRITE_ERROR:
            return self.write_error()

        if code in self.SPECIAL_ERROR_CODES:
            return self.special_error_code()

        if code in self.LOG_ONLY_CODES:
            return self.log_only_code()

        # Default case: no action needed
        return self.data

    def read_error(self):
        error_msg = self.data.sprintf[1]
        log_func, debug_msg = self.READ_ERROR_MAP.get(error_msg, (logging.warning, None))

        if debug_msg:
            logging.debug(debug_msg)

        # Choose message to log — prefer full message if debug_msg exists
        log_message = self.data.message if debug_msg else error_msg
        log_func(log_message)

        return MakeMKVErrorMessage(*dataclasses.astuple(self.data), self.data.message)

    def write_error(self):
        error_msg = self.data.sprintf[1]
        if error_msg == "Posix error - No such file or directory":
            logging.critical(self.data.message)
        else:
            logging.warning(error_msg)

        return MakeMKVErrorMessage(*dataclasses.astuple(self.data), self.data.message)

    def special_error_code(self):
        return MakeMKVErrorMessage(*dataclasses.astuple(self.data), self.data.message)

    def log_only_code(self):
        log_func = self.LOG_ONLY_CODES[self.data.code]
        log_func(self.data.message)
        return self.data


def run(options, select):
    """
    Run makemkv with input cli options and yield selected messages

    Parameters:
        options (list): makemkvcon cli options
        select (OutputType): output Message Type(s)
    Yields:
        dataclasses of selected type
    Raises:
        MakeMkvRuntimeError on makemkvcon exit code
    """
    if not isinstance(options, (tuple, list)):
        raise TypeError(options)
    if not isinstance(select, OutputType):
        raise TypeError(select)
    # Check makemkvcon path, resolves baremetal unique install issues
    # Docker container uses /usr/local/bin/makemkvcon
    makemkvcon_path = shutil.which("makemkvcon") or "/usr/local/bin/makemkvcon"
    # robot process of makemkvcon with
    cmd = [
        makemkvcon_path,
        "--robot",
        "--messages=-stdout",
    ]
    cmd += list(options)
    buffer = []
    logging.debug(f"command: '{' '.join(cmd)}'")
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
        logging.debug(f"PID {proc.pid}: command: '{' '.join(cmd)}'")
        for line in proc.stdout:
            line = line.rstrip(os.linesep)
            logging.debug(line)  # Maybe write the raw output to a separate log
            if proc.returncode:
                buffer.append(line)
                continue
            try:
                msg_type, data = parse_line(line)
            except MakeMkvParserError as err:
                logging.warning(err)
                buffer.append(line)
                continue
            logging.debug(data)
            if msg_type in select:
                yield data
    if proc.returncode:
        raise MakeMkvRuntimeError(proc.returncode, cmd, output=os.linesep.join(buffer))
    if buffer:
        logging.warning(f"Cannot parse {len(buffer)} lines: {os.linesep.join(buffer)}")
        raise MakeMkvRuntimeError(proc.returncode, cmd, output=os.linesep.join(buffer))
    logging.info("MakeMKV exits gracefully.")


def manual_wait(job) -> bool:
    """
    Pause execution to allow for user interaction and monitor job readiness.

    This function initiates a manual wait mode for a specified job, notifying the user
    to configure job parameters within a set time limit. The function sends periodic
    reminders and checks the job's readiness state. If the job is set to `manual_start`
    before the timeout, it exits early; otherwise, it continues until time expires.

    Parameters:
        job (Job): An instance of the job to monitor, which includes attributes
                   such as `job_id` and `manual_start` indicating job readiness.

    Returns:
        bool: `True` if the user sets the job to ready (`manual_start` is enabled)
              within the wait time, otherwise `False`.

    Notes:
        - The function checks in one minute intervals for state changes
        - A reminder is sent every 10 minutes.
        - A final notification is sent when one minute is left, warning of potential
          cancellation.
    """
    user_ready = False
    wait_time: int = 30

    title = "Manual Mode Activated!"
    message = f"ARM has taken it's hands off the wheels. You have {wait_time} minutes to set the job."
    notify(job, title, message)

    # Wait for the user to set the files and then start
    title = "Waiting for input on job!"
    for i in range(wait_time, 0, -1):
        # Wait for a minute
        sleep(60)

        # Refresh job data
        db.session.refresh(job)
        logging.debug(f"Wait time logging: [{i}] mins - Ready: [{job.manual_start}]")

        # Check the job state (true once ready)
        if job.manual_start:
            user_ready = True
            title = "The Wait is Over"
            message = "Thanks for not forgetting me, I am now processing your job."
            notify(job, title, message)
            break
        else:
            # If nothing has happened, remind the user every 5 minutes
            if i % 5 == 0 and i != wait_time:
                body = f"Don't forget me, I need your help to continue doing ARM things!. You have {i} minutes."
                notify(job, title, body)

            if i == 1:
                body = "ARM is about to cancel this job!!! You have less than 1 minute left!"
                notify(job, title, body)

    return user_ready
