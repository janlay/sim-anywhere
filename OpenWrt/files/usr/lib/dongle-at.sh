# dongle-at.sh: AT session helpers for the 4G dongle, sourced by sim-anywhere-detection and the SMS CGI.
# Callers serialize access with: exec 9>"$AT_LOCK"; flock 9
AT_LOCK=/var/lock/sim-anywhere-detection.lock
ATCACHE=/tmp/dongle-atport
SMS=/root/dongle-sms.tsv     # SMS archive: uid <TAB> in|out <TAB> epoch <TAB> read 0|1 <TAB> PDU (hex)

# AT session on fd 3. The tty must be set raw while it is open: termios resets on last close,
# and a cooked tty echoes modem output back to the modem and corrupts the responses.
CR=$(printf '\r')
at_open() {
  { exec 3<>"$1"; } 2>/dev/null || return 1
  stty raw -echo <&3 2>/dev/null || { exec 3>&-; return 1; }
  timeout 0.3 cat <&3 >/dev/null 2>&1   # drop stale bytes
  at_cmd ATE0 >/dev/null                 # no command echo
  return 0
}
at_close() { exec 3>&-; }
# send one AT command and print its response (AT_T: seconds to wait for each line, default 3)
at_cmd() {
  printf '%s\r' "$1" >&3
  at_read "$1"
}
# print response lines up to the final result code, skipping blank lines and the echo of $1
at_read() {
  while IFS= read -r -t "${AT_T:-3}" l <&3; do
    l=${l%"$CR"}
    case "$l" in ""|"$1") continue ;; esac
    echo "$l"
    case "$l" in OK|ERROR|*"CME ERROR"*|*"CMS ERROR"*) break ;; esac
  done
}
# 0 when the tty answers ATI with OK (session stays open on success)
at_probe() { at_open "$1" && at_cmd ATI | grep -q '^OK$' && return 0; at_close; return 1; }

# move all SMS from modem storage into the archive (PDU mode, raw PDUs kept for the page to decode)
sms_sync() {
  at_cmd 'AT+CMGF=0' >/dev/null
  at_cmd 'AT+CMGL=4' | awk '/^\+CMGL:/{split($2,a,","); i=a[1]; if((getline p)>0) print i, p}' > /tmp/dongle-sms.list
  while read -r i pdu; do
    case "$pdu" in *[!0-9A-Fa-f]*|"") continue ;; esac
    grep -q "	$pdu\$" "$SMS" 2>/dev/null ||
      printf '%s\tin\t%s\t0\t%s\n' "$(echo "$pdu" | md5sum | cut -c1-12)" "$(date +%s)" "$pdu" >> "$SMS" || continue
    at_cmd "AT+CMGD=$i" >/dev/null
  done < /tmp/dongle-sms.list
  rm -f /tmp/dongle-sms.list
}
