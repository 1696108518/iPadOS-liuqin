#!/usr/bin/env python3
"""Apply the LIUQIN bring-up changes to an iPadOS root made by tools/mkiosroot.sh.

  sudo liuqin-root-tweaks.py MOUNTED_ROOT

MOUNTED_ROOT is the HFS+ image mounted read-write (Linux: run
`fsck.hfsplus -fy` on the image, then `mount -t hfsplus -o loop,force`).
Run as root (it writes root/mobile owned files). Idempotent. This is what
turned the plain mkiosroot.sh output into the images that reach the home
screen (image versions v8-v14 in NOTES.md):

  launchd.plist   _PanicOnCrash -> _PanicOnCrasX (a crashing daemon must not
                  panic the system); CommCenter jobs removed (no baseband: it
                  crash-loops and Setup blocks on its CoreTelephony XPC)
  containers      /private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}
                  (on iOS they live on the data volume)
  migration       LastSystemVersion so DataMigration does not run for an hour
                  on every boot
  audio           VirtualAudio HAL plug-in disabled (mediaserverd crash)
  MediaToolbox    StartRecorderServer=false (camera filter prewarm calls a
                  NULL OpenGL function without a GPU -> mediaserverd crash loop)
  SpringBoard     BKNoWatchdogs (one slow CPU path, no scene-create watchdog
                  kills), no cloud-configuration UI
  Setup           skipped: SetupDone, SetupFinishedAllSteps, SetupVersion 11,
                  mini-buddy flags off
  activation      mobileactivationd data ark seeded; the actual Activated state
                  comes from its Hactivation branch, selected by the /chosen
                  security-status properties in tools/mkadt.py
"""
import glob, os, plistlib, shutil, sys

# Fallback only: the real values are read from the root's SystemVersion.plist
# (any iPadOS 16.x build for the iPad8,1 identity in tools/mkadt.py).
BUILD, VERSION = '20H392', '16.7.16'
MOBILE = 501


def safe_chown(path, uid, gid):
    """chown that tolerates a volume mounted without owner support (macOS)."""
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def load(path):
    with open(path, 'rb') as f:
        return plistlib.load(f)


def save(path, data, uid, gid, mode, fmt=plistlib.FMT_BINARY):
    with open(path, 'wb') as f:
        plistlib.dump(data, f, fmt=fmt)
    safe_chown(path, uid, gid)
    os.chmod(path, mode)


def merge_pref(root, name, values):
    """Merge keys into /var/mobile/Library/Preferences/NAME.plist (mobile, 0600)."""
    path = os.path.join(root, 'private/var/mobile/Library/Preferences', name + '.plist')
    data = load(path) if os.path.exists(path) else {}
    data.update(values)
    save(path, data, MOBILE, MOBILE, 0o600)
    print('pref', name, sorted(values))


def launchd(root):
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    renamed = 0
    for job in jobs.values():
        if '_PanicOnCrash' in job:
            job['_PanicOnCrasX'] = job.pop('_PanicOnCrash')
            renamed += 1
    removed = [k for k, j in jobs.items() if j.get('Label') == 'com.apple.CommCenter']
    for k in removed:
        del jobs[k]
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print(f'launchd.plist: {renamed} _PanicOnCrash renamed, {len(removed)} CommCenter jobs removed')


def containers(root):
    base = os.path.join(root, 'private/var/containers')
    for sub, uid in (('Bundle', 33), ('Data/System', 0), ('Shared/SystemGroup', 0), ('Temp', 0)):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
            safe_chown(d, uid, uid)
            os.chmod(d, 0o755)
            print('mkdir', d)


def find_container(root, kind, identifier):
    for meta in glob.glob(os.path.join(root, 'private/var/containers', kind, '*',
                                       '.com.apple.mobile_container_manager.metadata.plist')):
        if load(meta).get('MCMMetadataIdentifier') == identifier:
            return os.path.dirname(meta)
    return None


def activation(root):
    c = find_container(root, 'Data/System', 'com.apple.mobileactivationd')
    if c is None:
        print('warning: no mobileactivationd system container in the image; skipped data ark')
        return
    d = os.path.join(c, 'Library/internal')
    os.makedirs(d, exist_ok=True)
    safe_chown(d, MOBILE, MOBILE)
    os.chmod(d, 0o755)
    save(os.path.join(d, 'data_ark.plist'), {
        '-ActivationState': 'Activated', '-BrickState': False,
        '-BuildVersion': BUILD, '-ProductVersion': VERSION,
        '-DataMigrationCompleted': True, '-PreSydroDataMigrationCompleted': True,
    }, MOBILE, MOBILE, 0o600)
    print('data ark', d)


def audio(root):
    src = os.path.join(root, 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin')
    dst_dir = os.path.join(root, 'Library/Audio/Plug-Ins/HAL.disabled')
    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst_dir)
        print('VirtualAudio.plugin disabled')
    for conf in glob.glob(os.path.join(root, 'System/Library/Frameworks/MediaToolbox.framework/*/SubServerConfiguration.plist')):
        st = os.stat(conf)
        data = load(conf)
        data['StartRecorderServer'] = False
        save(conf, data, st.st_uid, st.st_gid, st.st_mode & 0o7777, plistlib.FMT_XML)
        print('StartRecorderServer off', os.path.relpath(conf, root))


def system_version(root):
    """(build, version) of the mounted root, so LastSystemVersion matches it."""
    path = os.path.join(root, 'System/Library/CoreServices/SystemVersion.plist')
    try:
        with open(path, 'rb') as f:
            data = plistlib.load(f)
        return data.get('ProductBuildVersion', BUILD), data.get('ProductVersion', VERSION)
    except (OSError, plistlib.InvalidFileException):
        return BUILD, VERSION


def gpu_test(root):
    """Install the userspace ABI smoke test and a launchd job that runs it once
    at boot with stdout on /dev/console, so its result lands in the persistent
    kernel log.  The binary is built by tools/build-gpu-test.sh."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    binary = os.path.join(repo, 'shim/out/liuqin-gpu-test')
    if not os.path.exists(binary):
        print('gpu-test: shim/out/liuqin-gpu-test not built, skipping')
        return
    tool = os.path.join(root, 'usr/libexec/liuqin-gpu-test')
    os.makedirs(os.path.dirname(tool), exist_ok=True)
    shutil.copyfile(binary, tool)
    safe_chown(tool, 0, 0)
    os.chmod(tool, 0o755)
    job = os.path.join(root, 'System/Library/LaunchDaemons/com.liuqin.gputest.plist')
    payload = plistlib.dumps({
        'Label': 'com.liuqin.gputest',
        'ProgramArguments': ['/usr/libexec/liuqin-gpu-test'],
        'RunAtLoad': True,
        'KeepAlive': False,
        'ThrottleInterval': 60,
        'StandardOutPath': '/dev/console',
        'StandardErrorPath': '/dev/console',
    }, fmt=plistlib.FMT_BINARY)
    with open(job, 'wb') as f:
        f.write(payload)
    safe_chown(job, 0, 0)
    os.chmod(job, 0o644)
    print('gpu-test: installed /usr/libexec/liuqin-gpu-test and its launchd job')


def main():
    if len(sys.argv) != 2 or not os.path.isdir(os.path.join(sys.argv[1], 'System/Library/xpc')):
        sys.exit(__doc__)
    root = sys.argv[1]
    build, version = system_version(root)
    print(f'root is iPadOS {version} ({build})')
    gpu_test(root)
    launchd(root)
    containers(root)
    activation(root)
    audio(root)
    merge_pref(root, 'com.apple.migration', {
        'LastSystemVersion': build,
        'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': build}})
    merge_pref(root, 'com.apple.springboard', {
        'BKNoWatchdogs': True,
        'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
        'MCForceShowCloudConfigurationUI': False})
    merge_pref(root, 'com.apple.purplebuddy', {
        'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11,
        'daemonizeNeedsToRun': False})
    merge_pref(root, 'com.apple.purplebuddy.notbackedup', {
        k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
                           'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy',
                           'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
                           'SilentICDPUpgrade')})
    merge_pref(root, 'com.apple.managedconfiguration.notbackedup', {
        'LockdownActivationIndicatesCloudConfigurationAvailable': False})


if __name__ == '__main__':
    main()
