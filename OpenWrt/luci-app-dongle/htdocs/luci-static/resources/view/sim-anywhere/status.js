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

function getPushDevices() {
	return fs.exec_direct('/usr/bin/dongle-status', [ 'push-devices' ], 'json');
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

function pushDeviceTable(data, refresh) {
	if (!data || data.error)
		return E('p', { 'class': 'alert-message warning' }, _('手机注册状态读取失败，请检查来电推送服务是否已更新安装。'));
	var reasons = {
		unconfigured: _('APNs 未配置或密钥不可用'),
		signing_unavailable: _('节点当前不具备 APNs 签名能力'),
		missing_metadata: _('缺少注册信息，请更新并打开 App'),
		bundle_mismatch: _('Bundle ID 不匹配'),
		team_mismatch: _('签名团队不匹配'),
		invalid_environment: _('推送环境无效'),
		invalid_token: _('设备 token 无效'),
		token_expired: _('APNs 已注销 token，请等待手机更新注册')
	};
	var devices = data.devices || [];
	var rows = devices.map(function(d) {
		var state = d.state === 'accepted' ? _('APNs 已接受真实来电推送')
			: d.state === 'pending' ? _('配置匹配，待真实来电验证')
			: d.state === 'rejected' ? (d.apns_code ? _('APNs 拒绝：HTTP %s').format(d.apns_code) : _('推送连接失败'))
			: (reasons[d.reason] || _('注册无效'));
		var select = E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'disabled': d.selected || d.state === 'invalid',
			'click': ui.createHandlerFn(null, function() {
				return fs.exec_direct('/usr/bin/sim-anywhere-push', [ 'select', d.device ], 'json')
					.then(function(r) {
						if (!r || !r.ok) throw new Error(_('切换失败，请刷新设备状态'));
						return refresh();
					}).catch(function(err) { ui.addNotification(null, E('p', String(err)), 'error'); });
			})
		}, d.selected ? _('当前接收设备') : _('设为接收设备'));
		var remove = E('button', {
			'class': 'btn cbi-button cbi-button-remove',
			'style': 'margin-left: 6px',
			'click': ui.createHandlerFn(null, function() {
				if (!confirm(_('移除后该手机不再接收来电通知，直到它重新注册。确定移除？')))
					return;
				return fs.exec_direct('/usr/bin/sim-anywhere-push', [ 'remove', d.device ], 'json')
					.then(function(r) {
						if (!r || !r.ok) throw new Error(_('移除失败，请刷新设备状态'));
						return refresh();
					}).catch(function(err) { ui.addNotification(null, E('p', String(err)), 'error'); });
			})
		}, _('移除'));
		return E('tr', { 'class': 'tr' }, [
			E('td', { 'class': 'td' }, d.name || d.model || _('手机')),
			E('td', { 'class': 'td' }, (d.model || '-') + ' / iOS ' + (d.os_version || '-')),
			E('td', { 'class': 'td' }, [ E('div', {}, [
					d.matched ? E('span', { 'style': 'color: #2e7d32', 'title': _('与节点配置匹配') }, '✓ ') : '',
					(d.app_version || '-') + ' (' + (d.app_build || '-') + ')' ]),
				E('small', {}, d.environment || '-') ]),
			E('td', { 'class': 'td' }, state),
			E('td', { 'class': 'td' }, formatTime(d.updated_at)),
			E('td', { 'class': 'td' }, [ select, remove ])
		]);
	});
	if (!rows.length)
		rows.push(E('tr', { 'class': 'tr' }, E('td', { 'class': 'td', 'colspan': 6 }, _('尚无手机注册，请打开 App 并连接节点'))));
	return table([ _('手机'), _('机型 / 系统'), _('App / 注册信息'), _('验证状态'), _('最近注册'), _('来电接收') ], rows);
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
		return Promise.all([ getStatus(), getClash(), getPushDevices() ]);
	},

	render: function(res) {
		var st = res[0], clash = res[1], pushDevices = res[2];
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
				draw(st, clash, pushDevices);
			}

			function run(cmd) {
				if (cmd === 'egress-ip') egress = { pending: true }; else latency = { pending: true };
				draw(st, clash, pushDevices);
				return fs.exec_direct('/usr/bin/dongle-status', [ cmd ], 'json').then(function(r) {
					store(cmd, r || { ok: false, error: '无返回' });
				}, function(err) {
					store(cmd, { ok: false, error: String(err) });
				});
			}

			return { egressText: egressText, latencyText: latencyText, run: run };
		})();

		function refresh() {
			return Promise.all([ getStatus(), getClash(), getPushDevices() ]).then(function(r) {
				st = r[0]; clash = r[1]; pushDevices = r[2];
				draw(st, clash, pushDevices);
			});
		}

		function draw(data, clashState, devices) {
			var services = data.services.map(function(s) { return serviceRow(s, refresh); });

			box.innerHTML = '';
			box.appendChild(deviceStatus(data, tests));
			box.appendChild(E('h3', {}, _('来电接收手机')));
			box.appendChild(E('p', { 'class': 'cbi-value-description' },
				_('切换从下一通来电开始生效，前台和后台均只通知所选手机。配置匹配仍然需要真实来电验证。')));
			box.appendChild(pushDeviceTable(devices, refresh));
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

		draw(st, clash, pushDevices);
		poll.add(refresh, 10);

		// First measurement right away, so the rows are not empty when the page opens.
		if (st.present === 1)
			tests.run('egress-ip').then(function() { return tests.run('latency'); });

		return box;
	}
});
