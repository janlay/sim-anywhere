'use strict';
'require view';
'require poll';
'require rpc';
'require fs';
'require ui';

var callSetInitAction = rpc.declare({
	object: 'luci',
	method: 'setInitAction',
	params: [ 'name', 'action' ]
});

var names = {
	'sim-anywhere-calld': _('语音通话服务 (sim-anywhere-calld)'),
	'sim-anywhere-vowifid': _('Wi-Fi Calling 服务 (sim-anywhere-vowifid)'),
	'sim-anywhere-quota': _('流量控制 (sim-anywhere-quota)'),
	'uhttpd': _('Web 服务 (uhttpd：LuCI + API)'),
	'sim-anywhere-detection': _('状态采集 (sim-anywhere-detection)')
};

function getStatus() {
	return fs.exec_direct('/usr/bin/dongle-status', [ 'status' ], 'json');
}

function getClash() {
	return fs.exec_direct('/usr/bin/dongle-clash', [ 'status' ], 'json');
}

function formatTime(epoch) {
	return epoch ? new Date(epoch * 1000).toLocaleString() : '-';
}

function serviceRow(svc, refresh) {
	var ctl = [];

	if (svc.type === 'procd' && svc.name !== 'uhttpd') {
		ctl.push(E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'click': ui.createHandlerFn(null, function() {
				return callSetInitAction(svc.name, 'restart').then(function() {
					ui.addNotification(null, E('p', _('已重启 %s').format(svc.name)), 'info');
				});
			})
		}, _('重启')));
	}

	// The collector has no service behind it: it is one line in /etc/crontabs/root, which is
	// easy to delete or comment out by hand and fails silently when it is gone.
	if (svc.type === 'cron' && svc.problem) {
		ctl.push(E('button', {
			'class': 'btn cbi-button cbi-button-apply',
			'click': ui.createHandlerFn(null, function() {
				return fs.exec_direct('/usr/bin/dongle-status', [ 'repair' ], 'json').then(function(r) {
					if (r && r.ok) {
						ui.addNotification(null, E('p', _('已恢复计划任务：%s').format(r.line)), 'info');
						if (refresh) refresh();
					}
					else {
						ui.addNotification(null, E('p', _('修复失败：%s').format((r && r.problem) || '?')), 'danger');
					}
				});
			})
		}, _('修复')));
	}

	var state;
	if (svc.type === 'cron')
		state = svc.problem
			? [ E('div', {}, _('异常')), E('div', { 'class': 'cbi-value-description' }, svc.problem) ]
			: _('正常');
	else
		state = svc.running ? _('运行中') : _('已停止');

	return E('tr', { 'class': 'tr' }, [
		E('td', { 'class': 'td' }, names[svc.name] || svc.name),
		E('td', { 'class': 'td' }, state),
		E('td', { 'class': 'td' }, svc.type === 'cron' ? formatTime(svc.last_run) : (svc.pid || '-')),
		E('td', { 'class': 'td' }, svc.gid != null ? String(svc.gid) : '-'),
		E('td', { 'class': 'td' }, ctl)
	]);
}

function clashTable(c) {
	var integrated = !!(c && c.integrated);
	var state = integrated ? (c.running ? _('已集成') : _('已停止')) : _('未集成');
	var core = c && c.core ? c.core + (c.version ? ' ' + c.version : '') : '-';
	var pid = integrated && c.pid ? String(c.pid) : '-';
	var gid = integrated && c.gid != null ? String(c.gid) : '-';
	var exit = '-';
	if (integrated) {
		if (c.exit)
			exit = c.exit + (c.interface ? ' (' + c.interface + ')' : '');
		else
			exit = _('未找到 4G 出口代理');
	}

	var rows = [
		[ _('状态'), state ],
		[ _('核心'), core ],
		[ _('PID'), pid ],
		[ _('GID'), gid ],
		[ _('4G 出口'), exit ]
	];
	if (c && c.error)
		rows.push([ _('错误'), c.error ]);

	return table([ _('项目'), _('值') ], rows.map(function(r) {
		return E('tr', { 'class': 'tr' }, [
			E('td', { 'class': 'td' }, r[0]),
			E('td', { 'class': 'td' }, r[1])
		]);
	}));
}

function clientRows(list) {
	if (!list.length)
		return [ E('tr', { 'class': 'tr' }, [ E('td', { 'class': 'td', 'colspan': 2 }, _('无连接')) ]) ];

	return list.map(function(c) {
		return E('tr', { 'class': 'tr' }, [
			E('td', { 'class': 'td' }, c.ip),
			E('td', { 'class': 'td' }, String(c.connections))
		]);
	});
}

function netText(net, present) {
	if (!present)
		return '-';
	if (net.sim === 'no_sim')
		return _('未插 SIM 卡');
	if (net.sim === 'locked')
		return _('SIM 卡已锁定');
	if (!net.oper)
		return _('未入网');
	var state = net.reg == 5 ? _('漫游') : net.reg == 1 ? _('本网') : _('注册中');
	return net.oper + ' · ' + state;
}

function deviceStatus(data, tests) {
	var present = data.present === 1;
	var rows = [
		{ label: _('网络接口'), value: present ? (data.iface || '-') : '-' },
		{ label: _('设备型号'), value: data.model || (present ? _('不可识别') : _('未插入')) },
		{ label: _('入网状态'), value: netText(data.net || {}, present) },
		{ label: _('出口 IP'), cmd: 'egress-ip', render: tests.egressText },
		{ label: _('网络延迟'), cmd: 'latency', render: tests.latencyText }
	];

	return E('div', {}, [
		E('h3', {}, _('设备状态')),
		table([ _('项目'), _('值') ], rows.map(function(r) {
			if (r.cmd)
				return E('tr', { 'class': 'tr' }, [
					E('td', { 'class': 'td' }, r.label),
					E('td', { 'class': 'td' }, [
						E('span', { 'id': 'dongle-' + r.cmd }, r.render()),
						E('button', {
							'class': 'btn cbi-button cbi-button-action',
							'style': 'margin-left: 6px',
							'title': _('重新测试'),
							'click': ui.createHandlerFn(null, function() {
								return tests.run(r.cmd);
							})
						}, '⟳')
					])
				]);
			return E('tr', { 'class': 'tr' }, [
				E('td', { 'class': 'td' }, r.label),
				E('td', { 'class': 'td' }, r.value)
			]);
		}))
	]);
}

function table(head, rows) {
	return E('table', { 'class': 'table' }, [
		E('tr', { 'class': 'tr table-titles' }, head.map(function(h) {
			return E('th', { 'class': 'th' }, h);
		}))
	].concat(rows));
}

return view.extend({
	load: function() {
		return Promise.all([ getStatus(), getClash() ]);
	},

	render: function(res) {
		var st = res[0], clash = res[1];
		var box = E('div', {}, []);

		// 出口 IP and 网络延迟 hit the internet over the modem, so they are never part of
		// the 10 s poll: they run only when the refresh button is pressed (and once on
		// load). The results live here, outside draw(), so a redraw keeps showing them.
		var tests = (function() {
			var egress = null, latency = null;

			function egressText() {
				if (!egress) return _('未测试');
				if (egress.pending) return _('测试中…');
				return egress.ok ? egress.value : _('失败：%s').format(egress.error);
			}

			function latencyText() {
				if (!latency) return _('未测试');
				if (latency.pending) return _('测试中…');
				return latency.ok
					? latency.ms + ' ms（HTTP ' + latency.code + '）'
					: _('失败：%s').format(latency.error);
			}

			function store(cmd, r) {
				if (cmd === 'egress-ip') egress = r; else latency = r;
				draw(st, clash);
			}

			function run(cmd) {
				if (cmd === 'egress-ip') egress = { pending: true }; else latency = { pending: true };
				draw(st, clash);
				return fs.exec_direct('/usr/bin/dongle-status', [ cmd ], 'json').then(function(r) {
					store(cmd, r || { ok: false, error: '无返回' });
				}, function(err) {
					store(cmd, { ok: false, error: String(err) });
				});
			}

			return { egressText: egressText, latencyText: latencyText, run: run };
		})();

		function refresh() {
			return Promise.all([ getStatus(), getClash() ]).then(function(r) {
				draw(r[0], r[1]);
			});
		}

		function draw(data, clashState) {
			var services = data.services.map(function(s) { return serviceRow(s, refresh); });

			box.innerHTML = '';
			box.appendChild(deviceStatus(data, tests));
			box.appendChild(E('h3', {}, _('Clash 集成 (mihomo/clash)')));
			box.appendChild(clashTable(clashState));
			box.appendChild(E('h3', {}, _('服务')));
			box.appendChild(table([ _('服务'), _('状态'), _('PID / 时间'), _('GID'), _('操作') ], services));
			if (data.present !== 1)
				box.appendChild(E('p', { 'class': 'cbi-value-description' },
					_('未检测到设备：只有状态采集在运行。sim-anywhere-calld、sim-anywhere-vowifid 和节点发现服务都已停止，API 对需要设备的请求返回 503；插入设备后会自动启动。')));
			box.appendChild(E('h3', {}, _('API 客户端（HTTP，端口 80，包含 LuCI）')));
			box.appendChild(table([ _('IP'), _('连接数') ], clientRows(data.clients.http)));
			box.appendChild(E('h3', {}, _('通话客户端（WebSocket，端口 8765）')));
			box.appendChild(table([ _('IP'), _('连接数') ], clientRows(data.clients.calls)));
		}

		draw(st, clash);
		poll.add(refresh, 10);

		// First measurement right away, so the rows are not empty when the page opens.
		if (st.present === 1)
			tests.run('egress-ip').then(function() { return tests.run('latency'); });

		return box;
	}
});
