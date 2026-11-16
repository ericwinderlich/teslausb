#!/usr/bin/python3
# TeslaUSB settings endpoint.
#   GET              -> JSON with the current settings (passwords are never returned)
#   POST action=save     -> write settings, then reboot and apply them
#   POST action=handover -> forget all wifi networks, enable setup hotspot, power off
#
# Applying is done by /root/bin/teslausb-settings-apply at boot (see
# run/teslausb-settings-apply): it connects to the (new) wifi, or opens
# the "TeslaUSB-Setup" hotspot if no wifi is reachable, and re-runs
# "setup-teslausb upgrade" when the archive settings changed.

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse

CONF = '/root/teslausb_setup_variables.conf'
BOOT = '/teslausb'
DEFAULT_REPO = 'ericwinderlich'
DEFAULT_BRANCH = 'main-dev'
HOTSPOT_CON = 'TESLAUSB_SETUP'

# settings that need a "setup-teslausb upgrade" when they change
SETUP_VARS = ['ARCHIVE_SYSTEM', 'ARCHIVE_SERVER', 'SHARE_NAME', 'SHARE_USER',
              'SHARE_PASSWORD', 'CIFS_VERSION', 'TIME_ZONE', 'TESLAUSB_HOSTNAME']
CIFS_VARS = ['ARCHIVE_SERVER', 'SHARE_NAME', 'SHARE_USER', 'SHARE_PASSWORD', 'CIFS_VERSION']
SECRET_VARS = ['WIFIPASS', 'SHARE_PASSWORD']


def run(args, input_text=None):
    return subprocess.run(['sudo', '-n'] + args, capture_output=True, text=True, input=input_text)


def respond(status, obj):
    body = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write('HTTP/1.0 %s\r\nContent-type: application/json; charset=utf-8\r\n'
                     'Cache-Control: no-store\r\n\r\n' % status)
    sys.stdout.write(body)
    sys.stdout.flush()


def read_conf():
    r = run(['cat', CONF])
    text = r.stdout if r.returncode == 0 else ''
    values = {}
    for line in text.splitlines():
        m = re.match(r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
        if not m:
            continue
        name, raw = m.groups()
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            parts = [raw]
        values[name] = parts[0] if parts else ''
    return text, values


def write_conf(text, set_vars, unset_vars):
    """Remove every active line of the touched variables, then append the new values."""
    touched = set(set_vars) | set(unset_vars)
    out = []
    for line in text.splitlines():
        m = re.match(r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=', line)
        if m and m.group(1) in touched:
            continue
        if line.startswith('# --- set via web settings'):
            continue
        out.append(line)
    while out and out[-1].strip() == '':
        out.pop()
    if set_vars:
        out.append('')
        out.append('# --- set via web settings page ---')
        for name, value in set_vars.items():
            out.append('export %s=%s' % (name, shlex.quote(value)))
    new_text = '\n'.join(out) + '\n'

    # validate with bash before replacing the real file
    check = subprocess.run(['bash', '-n'], input=new_text, capture_output=True, text=True)
    if check.returncode != 0:
        raise RuntimeError('Konfiguration ungültig: ' + check.stderr.strip())

    run(['/root/bin/remountfs_rw'])
    tmp = CONF + '.new'
    r = run(['tee', tmp], input_text=new_text)
    if r.returncode != 0:
        raise RuntimeError('Konnte Konfiguration nicht schreiben: ' + r.stderr.strip())
    run(['chmod', '600', tmp])
    r = run(['mv', tmp, CONF])
    if r.returncode != 0:
        raise RuntimeError('Konnte Konfiguration nicht ersetzen: ' + r.stderr.strip())


def wifi_connections():
    r = run(['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'])
    names = []
    for line in r.stdout.splitlines():
        # nmcli escapes ':' in names as '\:'
        parts = re.split(r'(?<!\\):', line)
        if len(parts) >= 2 and parts[-1] == '802-11-wireless':
            name = ':'.join(parts[:-1]).replace('\\:', ':')
            if name not in (HOTSPOT_CON, 'TESLAUSB_AP'):
                names.append(name)
    return names


def network_state():
    state = {'connected_ssid': '', 'ip': '', 'hotspot_active': False, 'networks': []}
    r = run(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active'])
    for line in r.stdout.splitlines():
        parts = re.split(r'(?<!\\):', line)
        if len(parts) >= 3 and parts[1] == '802-11-wireless':
            name = parts[0].replace('\\:', ':')
            if name == HOTSPOT_CON:
                state['hotspot_active'] = True
            elif name != 'TESLAUSB_AP':
                ssid = run(['nmcli', '-g', '802-11-wireless.ssid', 'connection', 'show', name]).stdout.strip()
                state['connected_ssid'] = ssid or name
    r = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
    state['ip'] = r.stdout.strip()
    r = run(['nmcli', '-t', '-f', 'SSID,SIGNAL', 'device', 'wifi', 'list', '--rescan', 'no'])
    seen = {}
    for line in r.stdout.splitlines():
        parts = re.split(r'(?<!\\):', line)
        if len(parts) >= 2 and parts[0]:
            ssid = parts[0].replace('\\:', ':')
            try:
                sig = int(parts[1])
            except ValueError:
                sig = 0
            if ssid != 'TeslaUSB-Setup' and sig > seen.get(ssid, -1):
                seen[ssid] = sig
    state['networks'] = [{'ssid': s, 'signal': v} for s, v in sorted(seen.items(), key=lambda x: -x[1])]
    return state


def boot_flag(name):
    return os.path.exists(os.path.join(BOOT, name))


def do_get():
    _, v = read_conf()
    data = {
        'ssid': v.get('SSID', ''),
        'wifipass_set': bool(v.get('WIFIPASS')),
        'archive_system': v.get('ARCHIVE_SYSTEM', 'none') or 'none',
        'archive_server': v.get('ARCHIVE_SERVER', ''),
        'share_name': v.get('SHARE_NAME', ''),
        'share_user': v.get('SHARE_USER', ''),
        'share_password_set': bool(v.get('SHARE_PASSWORD')),
        'cifs_version': v.get('CIFS_VERSION', ''),
        'time_zone': v.get('TIME_ZONE', ''),
        'hostname': v.get('TESLAUSB_HOSTNAME', 'teslausb'),
        'repo': v.get('REPO', 'marcone'),
        'branch': v.get('BRANCH', 'main-dev'),
        'setup_finished': boot_flag('TESLAUSB_SETUP_FINISHED'),
        'setup_hotspot_flag': boot_flag('SETUP_HOTSPOT'),
        'reconfigure_pending': boot_flag('TESLAUSB_RECONFIGURE'),
    }
    data.update(network_state())
    respond('200 OK', data)


def schedule(command):
    """Run a command a few seconds after the HTTP response has been sent."""
    subprocess.Popen(['sudo', '-n', 'systemd-run', '--on-active=4', '--timer-property=AccuracySec=1s',
                      '--unit=teslausb-settings-%d' % os.getpid()] + command,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def do_save(form):
    text, cur = read_conf()
    set_vars = {}
    unset_vars = []
    errors = []

    def field(name):
        return form.get(name, [''])[0].strip()

    ssid = field('ssid')
    wifipass = form.get('wifipass', [''])[0]   # passwords keep spaces
    if not ssid:
        errors.append('Bitte einen WLAN-Namen eingeben.')
    if wifipass and len(wifipass) < 8:
        errors.append('Das WLAN-Passwort muss mindestens 8 Zeichen haben.')
    ssid_changed = ssid != cur.get('SSID', '')
    if ssid_changed and not wifipass:
        errors.append('Für ein neues WLAN bitte auch das WLAN-Passwort eingeben.')

    archive = field('archive_system') or 'none'
    if archive not in ('none', 'cifs'):
        errors.append('Unbekanntes Archiv-System.')

    if archive == 'cifs':
        for key, label in (('archive_server', 'NAS-Adresse'), ('share_name', 'Freigabe'),
                           ('share_user', 'NAS-Benutzer')):
            if not field(key):
                errors.append('Bitte %s angeben.' % label)
        if not form.get('share_password', [''])[0] and not cur.get('SHARE_PASSWORD'):
            errors.append('Bitte das NAS-Passwort angeben.')

    tz = field('time_zone')
    if tz and not os.path.exists('/usr/share/zoneinfo/' + tz):
        errors.append('Unbekannte Zeitzone: %s' % tz)

    if errors:
        respond('400 Bad Request', {'ok': False, 'errors': errors})
        return

    set_vars['SSID'] = ssid
    if wifipass:
        set_vars['WIFIPASS'] = wifipass
    set_vars['ARCHIVE_SYSTEM'] = archive
    if archive == 'cifs':
        set_vars['ARCHIVE_SERVER'] = field('archive_server')
        set_vars['SHARE_NAME'] = field('share_name')
        set_vars['SHARE_USER'] = field('share_user')
        sp = form.get('share_password', [''])[0]
        if sp:
            set_vars['SHARE_PASSWORD'] = sp
        cv = field('cifs_version')
        if cv:
            set_vars['CIFS_VERSION'] = cv
        else:
            unset_vars.append('CIFS_VERSION')
    else:
        unset_vars.extend(CIFS_VARS)
    if tz:
        set_vars['TIME_ZONE'] = tz
    # make re-runs of setup fetch this fork, so the settings page survives
    if not cur.get('REPO'):
        set_vars['REPO'] = DEFAULT_REPO
    if not cur.get('BRANCH'):
        set_vars['BRANCH'] = DEFAULT_BRANCH

    # find out what actually changed
    new_values = dict(cur)
    for k in unset_vars:
        new_values.pop(k, None)
    new_values.update(set_vars)
    setup_changed = any(cur.get(k, '') != new_values.get(k, '') for k in SETUP_VARS) \
        or 'REPO' in set_vars or 'BRANCH' in set_vars
    wifi_changed = ssid_changed or bool(wifipass and wifipass != cur.get('WIFIPASS', ''))

    try:
        write_conf(text, set_vars, unset_vars)
    except RuntimeError as e:
        respond('500 Internal Server Error', {'ok': False, 'errors': [str(e)]})
        return

    if wifi_changed:
        # forget the old networks, rc.local connects to the new one on the next boot
        for name in wifi_connections():
            run(['nmcli', 'connection', 'delete', name])
        run(['rm', '-f', os.path.join(BOOT, 'WIFI_ENABLED')])
    if setup_changed:
        run(['touch', os.path.join(BOOT, 'TESLAUSB_RECONFIGURE')])
    run(['rm', '-f', os.path.join(BOOT, 'SETUP_HOTSPOT')])

    rebooting = wifi_changed or setup_changed or boot_flag('TESLAUSB_RECONFIGURE')
    if rebooting:
        schedule(['/sbin/reboot'])
    respond('200 OK', {'ok': True, 'wifi_changed': wifi_changed, 'setup_changed': setup_changed,
                       'rebooting': rebooting})


def do_handover():
    text, cur = read_conf()
    set_vars = {'ARCHIVE_SYSTEM': 'none'}
    if not cur.get('REPO'):
        set_vars['REPO'] = DEFAULT_REPO
    if not cur.get('BRANCH'):
        set_vars['BRANCH'] = DEFAULT_BRANCH
    try:
        write_conf(text, set_vars, ['SSID', 'WIFIPASS'] + CIFS_VARS)
    except RuntimeError as e:
        respond('500 Internal Server Error', {'ok': False, 'errors': [str(e)]})
        return
    for name in wifi_connections():
        run(['nmcli', 'connection', 'delete', name])
    run(['rm', '-f', os.path.join(BOOT, 'WIFI_ENABLED')])
    # the new owner's first save always re-runs setup, so no old archive config survives
    run(['touch', os.path.join(BOOT, 'SETUP_HOTSPOT'), os.path.join(BOOT, 'TESLAUSB_RECONFIGURE')])
    schedule(['/sbin/poweroff'])
    respond('200 OK', {'ok': True})


def main():
    method = os.environ.get('REQUEST_METHOD', 'GET')
    if method == 'GET':
        do_get()
        return
    if method != 'POST':
        respond('405 Method Not Allowed', {'ok': False, 'errors': ['Nur GET und POST.']})
        return
    length = int(os.environ.get('CONTENT_LENGTH') or 0)
    body = sys.stdin.buffer.read(length).decode('utf-8', 'replace') if length else ''
    form = urllib.parse.parse_qs(body, keep_blank_values=True)
    action = form.get('action', [''])[0]
    if action == 'save':
        do_save(form)
    elif action == 'handover':
        if form.get('confirm', [''])[0] != 'yes':
            respond('400 Bad Request', {'ok': False, 'errors': ['Bestätigung fehlt.']})
            return
        do_handover()
    else:
        respond('400 Bad Request', {'ok': False, 'errors': ['Unbekannte Aktion.']})


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # never leave nginx without an answer
        respond('500 Internal Server Error', {'ok': False, 'errors': ['Interner Fehler: %s' % e]})
