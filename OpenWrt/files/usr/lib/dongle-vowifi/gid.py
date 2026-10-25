# Group that OpenClash skips. Its output chain returns early for owner --gid-owner N, so traffic of processes in
# group N is not proxied. sim-anywhere-vowifid must use that group. The value comes from the firewall rule; UCI
# dongle.vowifi.gid overrides it.
import re
import subprocess

FALLBACK_GID = 65534
RULE = re.compile(r"-A openclash_output .*--gid-owner (\d+) -j RETURN")
NFT_CHAIN = re.compile(r"chain openclash\S*_output \{")
NFT_RETURN = re.compile(r"meta skgid (\d+) .*\breturn\b")


def run(cmd):
	try:
		return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
	except (OSError, subprocess.SubprocessError):
		return ""


def nft_gid():
	# nftables: "meta skgid N return" inside the openclash*_output chains
	in_chain = False
	for line in run(["nft", "list", "ruleset"]).splitlines():
		if NFT_CHAIN.search(line):
			in_chain = True
		elif line.strip() == "}":
			in_chain = False
		elif in_chain and (match := NFT_RETURN.search(line)):
			return int(match.group(1))
	return None


def iptables_gid():
	# iptables-save lists the rule in the mangle table (TPROXY) and in the nat table (DNS). Both use the same group.
	for table in ("mangle", "nat"):
		match = RULE.search(run(["iptables-save", "-t", table]))
		if match:
			return int(match.group(1))
	return None


def firewall_gid():
	return nft_gid() or iptables_gid()


def default_gid():
	return firewall_gid() or FALLBACK_GID


def effective_gid():
	uci = run(["uci", "-q", "get", "dongle.vowifi.gid"]).strip()
	return int(uci) if uci else default_gid()
