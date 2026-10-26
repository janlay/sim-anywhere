'use strict';
'require view';
'require form';
'require fs';
'require uci';
'require ui';

var SIZE_UNITS = [ 'B', 'KB', 'MB', 'GB', 'TB' ];
var SIZE_RE = /^(\d+(?:\.\d+)?)\s*([KMGT]?B)?$/i;
var SIZE_CHOICES = { 'MB': 'MB', 'GB': 'GB' };

function fmtSize(bytes) {
	var v = Math.max(0, bytes || 0), i = 0;
	while (v >= 1024 && i < SIZE_UNITS.length - 1) {
		v /= 1024;
		i++;
	}
	return (i == 0 || v >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + ' ' + SIZE_UNITS[i];
}

function usageBar(label, used, limit, note, action) {
	var pct = limit > 0 ? Math.min(100, used / limit * 100) : 0;
	var heading = E('div', { 'style': 'display:flex;align-items:center;gap:8px' },
		E('div', { 'style': 'flex:1;min-width:0' }, label + ': ' + fmtSize(used) + (limit > 0 ? ' / ' + fmtSize(limit) : '（未设置）') + (note ? '，' + note : '')));
	if (action)
		heading.appendChild(action);
	var bar = E('div', { 'style': 'width:100%;background:#ddd;height:10px;border-radius:5px;overflow:hidden' },
		E('div', { 'style': 'width:' + pct + '%;height:100%;background:' + (pct >= 100 ? '#d9534f' : '#5cb85c') }));

	return E('div', { 'style': 'margin:6px 0' }, [
		heading,
		bar
	]);
}

function quotaPanel(qs) {
	if (!qs)
		return E('em', {}, _('未知（sim-anywhere-quota 未运行）'));

	var ratio = qs.ratio > 0 ? qs.ratio : 100;
	var plan = qs.cycle_on ? qs.plan : 0, daily = qs.daily_on ? qs.daily : 0;
	var cycleLimit = plan > 0 ? Math.floor(plan * ratio / 100) : 0;
	var cycle = E('div');

	function showBar() {
		cycle.replaceChildren(usageBar(_('本周期'), qs.used, cycleLimit,
			plan > 0 && ratio < 100 ? _('套餐 %s × %d%%').format(fmtSize(plan), ratio) : null,
			plan > 0 ? E('a', {
				'href': '#', 'class': 'sa-calibrate-link',
				'style': 'flex:none;white-space:nowrap',
				'click': function(ev) { ev.preventDefault(); showSlider(); }
			}, _('校正')) : null));
		if (plan <= 0)
			cycle.appendChild(E('div', { 'style': 'margin:6px 0' }, _('未启用周期限制。')));
	}

	function showSlider() {
		var max = Math.max(1, Math.ceil(Math.max(plan, qs.used) / 1048576));
		var slider = E('input', {
			'type': 'range', 'min': 0, 'max': max, 'step': 1,
			'value': Math.round(qs.used / 1048576),
			'style': 'width:100%;accent-color:#5cb85c'
		});
		var value = E('span', {}, fmtSize(slider.value * 1048576));
		slider.addEventListener('input', function() {
			value.textContent = fmtSize(slider.value * 1048576);
		});
		var confirm = E('button', {
			'type': 'button', 'class': 'cbi-button cbi-button-apply',
			'click': function() {
				return fs.exec_direct('/usr/bin/sim-anywhere-quota', [ 'calibrate', String(slider.value) ]).then(function() {
					window.location.reload();
				}, function(err) {
					ui.addNotification(null, E('p', _('校正失败：%s').format(err)), 'error');
				});
			}
		}, _('确认校正'));
		var cancel = E('button', { 'type': 'button', 'class': 'cbi-button', 'click': showBar }, _('取消'));

		cycle.replaceChildren(E('div', { 'style': 'margin:6px 0' }, [
			E('div', {}, _('拖动到本周期实际已用流量，之后在此基础上累计：')),
			slider,
			E('div', { 'style': 'margin-top:6px' }, [ value, ' ', confirm, ' ', cancel ])
		]));
	}

	showBar();
	// as wide as the size and slider controls, not the full field column
	return E('div', { 'style': 'max-width:340px' }, [ cycle, usageBar(_('今日'), qs.day_used, daily) ]);
}

function sizeParts(text, defaultUnit) {
	var s = String(text == null ? '' : text).trim();
	var m = SIZE_RE.exec(s);
	if (!m)
		return { value: s, unit: defaultUnit };
	if (m[2] && m[2].toUpperCase() == 'TB')
		return { value: String(Number(m[1]) * 1024), unit: 'GB' };
	if (m[2] && m[2].toUpperCase() == 'KB')
		return { value: String(Number(m[1]) / 1024), unit: 'MB' };
	return { value: m[1], unit: (m[2] || defaultUnit).toUpperCase() };
}

function sizeOption(section, name, title, description, defaultUnit) {
	var o = section.option(form.Value, name, title, description);
	var states = {};

	o.rmempty = true;
	o.placeholder = '0';
	o.renderWidget = function(section_id, option_index, cfgvalue) {
		var parts = sizeParts(cfgvalue, defaultUnit);
		var input = new ui.Textfield(parts.value, {
			'id': this.cbid(section_id),
			'placeholder': '0',
			'optional': true,
			'validate': function(value) {
				var v = String(value == null ? '' : value).trim();
				return v === '' || /^\d+(?:\.\d+)?$/.test(v) || _('请输入数字');
			}
		});
		var unit = new ui.Select(parts.unit, SIZE_CHOICES, {
			'id': this.cbid(section_id) + '.unit',
			'widget': 'select'
		});
		states[section_id] = { input: input, unit: unit };
		return E('div', { 'class': 'sa-size', 'style': 'display:flex;align-items:center;gap:8px;flex:1;min-width:0;max-width:240px' }, [
			E('div', { 'style': 'flex:1;min-width:0;max-width:160px' }, input.render()),
			E('div', { 'style': 'flex:0 0 72px' }, unit.render())
		]);
	};
	o.formvalue = function(section_id) {
		var st = states[section_id];
		if (!st)
			return null;
		var v = st.input.getValue();
		if (v == null || String(v).trim() === '')
			return '';
		return String(v).trim() + ' ' + st.unit.getValue();
	};
	return o;
}

function sliderOption(section, name, title, description, min, max, unit, fallback) {
	var o = section.option(form.Value, name, title, description);
	var states = {};

	o.datatype = 'range(' + min + ',' + max + ')';
	o.rmempty = true;
	o.renderWidget = function(section_id, option_index, cfgvalue) {
		var v = parseInt(cfgvalue, 10);
		if (isNaN(v))
			v = fallback;
		v = Math.max(min, Math.min(max, v));
		var slider = E('input', {
			'id': this.cbid(section_id),
			'type': 'range', 'min': min, 'max': max, 'step': 1, 'value': v,
			'style': 'flex:1;accent-color:#5cb85c'
		});
		var value = E('span', { 'style': 'min-width:36px;text-align:right' }, v + unit);
		slider.addEventListener('input', function() {
			value.textContent = slider.value + unit;
		});
		states[section_id] = slider;
		return E('div', { 'style': 'display:flex;align-items:center;gap:12px;max-width:340px' }, [ slider, value ]);
	};
	o.formvalue = function(section_id) {
		var slider = states[section_id];
		return slider ? slider.value : null;
	};
	return o;
}

/* Option hints: after the map renders, every description moves from below the input to
   a ? icon behind the input, shown as a standard .cbi-tooltip bubble on click (hover
   still works on desktop). Subordinate options sit inside their parent's value area. */
var helpCss = false;

function closeHelp() {
	document.querySelectorAll('.sa-help.open').forEach(function(el) { el.classList.remove('open'); });
}

function helpIcon(text) {
	return E('span', {
		'class': 'cbi-tooltip-container sa-help',
		'tabindex': '0',
		'click': function(ev) {
			ev.stopPropagation();
			var open = !this.classList.contains('open');
			closeHelp();
			this.classList.toggle('open', open);
		},
		'keydown': function(ev) {
			if (ev.key == 'Enter' || ev.key == ' ') {
				ev.preventDefault();
				this.click();
			}
		}
	}, [
		E('span', { 'class': 'sa-help-icon' }, '?'),
		E('div', { 'class': 'cbi-tooltip' }, text)
	]);
}

function decorate(root) {
	root.classList.add('sa-settings');
	if (!helpCss) {
		helpCss = true;
		document.head.appendChild(E('style', {}, ''
			+ '.sa-settings .cbi-value{margin-left:0;padding-left:0}'
			+ '.sa-settings .sa-sub-options>.cbi-value>.cbi-value-title{text-align:left;flex:0 0 7em;width:7em}'
			+ '.sa-sub-options{margin-top:18px}'
			+ '.sa-sub-options>.cbi-value:last-child{margin-bottom:0}'
			+ '.sa-size input,.sa-size select{width:100%;min-width:0;box-sizing:border-box}'
			+ '.sa-calibrate-link{color:var(--primary-color-high);text-decoration:underline;cursor:pointer}'
			+ '.sa-inline{position:relative;display:flex;align-items:center;gap:8px;max-width:100%}'
			+ '.sa-inline>.cbi-checkbox{height:30px;display:inline-flex;align-items:center}'
			+ '.sa-help{position:relative;display:inline-flex;align-items:center;outline:none}'
			+ '.sa-help-icon{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;'
			+ 'border:1px solid currentColor;border-radius:50%;font-size:11px;font-weight:bold;color:#999;'
			+ 'cursor:help;line-height:1}'
			+ '.sa-inline .sa-help .cbi-tooltip.cbi-tooltip{left:0;top:100%;margin-top:4px;width:max-content;'
			+ 'max-width:min(360px,90vw);white-space:normal;text-align:left;pointer-events:none}'
			+ '.sa-help.open .cbi-tooltip{opacity:1}'));
		document.addEventListener('click', closeHelp);
	}
	root.querySelectorAll('.cbi-value-description').forEach(function(desc) {
		var field = desc.parentNode;
		desc.remove();
		// the widget and the ? icon share one row; inputs and selects give room to the icon
		var line = E('div', { 'class': 'sa-inline' });
		while (field.firstChild)
			line.appendChild(field.firstChild);
		line.appendChild(helpIcon(desc.textContent));
		field.appendChild(line);
	});
	// Move the original rows so LuCI retains their widgets and dependency attributes.
	[
		[ 'cycle_limit', [ 'plan', 'cycle_ratio', 'cycle_mode', 'reset_day', 'cycle_days', 'cycle_start' ] ],
		[ 'daily_limit', [ 'daily' ] ],
		[ '_discovery_toggle', [ 'stop_on_connect' ] ]
	].forEach(function(group) {
		var section = group[0] == '_discovery_toggle' ? 'discovery' : 'quota';
		var parent = root.querySelector('[id="cbi-dongle-' + section + '-' + group[0] + '"]');
		if (!parent)
			return;
		var children = E('div', { 'class': 'sa-sub-options' });
		group[1].forEach(function(name) {
			var row = root.querySelector('[id="cbi-dongle-' + section + '-' + name + '"]');
			if (row)
				children.appendChild(row);
		});
		parent.querySelector('.cbi-value-field').appendChild(children);
	});
}

return view.extend({
	load: function() {
		return Promise.all([
			uci.load('dongle'),
			fs.exec_direct('/usr/bin/dongle-status', [ 'gid' ]),
			fs.exec_direct('/usr/bin/dongle-announce', [ 'status' ]).catch(function() { return 'null'; }),
			fs.exec_direct('/usr/bin/sim-anywhere-quota', [ 'status' ]).catch(function() { return 'null'; }),
			fs.exec_direct('/usr/bin/sim-anywhere-push-key', [ 'status' ]).catch(function() { return 'missing'; })
		]);
	},

	render: function(data) {
		var m, s, o, st = null, qs = null;

		try { st = JSON.parse(data[2]); } catch (e) {}
		try { qs = JSON.parse(data[3]); } catch (e) {}

		m = new form.Map('dongle', _('SIM Anywhere 设置'));

		s = m.section(form.NamedSection, 'vowifi', 'vowifi', _('Wi-Fi Calling'),
			_('保存并应用后，在“状态”页重启 sim-anywhere-vowifid，新的 GID 才会生效。'));

		o = s.option(form.Value, 'gid', _('进程 GID'),
			_('sim-anywhere-vowifid 启动后切换到该组。留空时使用防火墙中 OpenClash 跳过规则（openclash_output --gid-owner）的组，即自动检测值。修改后须同步修改该规则。'));
		o.datatype = 'range(1,65535)';
		o.placeholder = String(data[1]).trim();
		o.rmempty = true;

		s = m.section(form.NamedSection, 'push', 'push', _('来电推送（APNs）'),
			_('仅适用于使用自己的 Apple 开发团队签名构建的 App。节点在来电时直连 Apple APNs。请先在 Apple Developer 开启 Push Notifications，再保存以下标识并上传对应的 APNs .p8 密钥。'));

		o = s.option(form.Value, 'team_id', _('Team ID'), _('Apple Developer 团队的 10 位标识。'));
		o.rmempty = true;
		o.validate = function(section_id, value) {
			return !value || /^[A-Z0-9]{10}$/.test(value) || _('请输入 10 位大写字母或数字');
		};

		o = s.option(form.Value, 'key_id', _('Key ID'), _('与下方 APNs .p8 密钥对应的 10 位标识。'));
		o.rmempty = true;
		o.validate = function(section_id, value) {
			return !value || /^[A-Z0-9]{10}$/.test(value) || _('请输入 10 位大写字母或数字');
		};

		o = s.option(form.Value, 'bundle_id', _('Bundle ID'), _('必须与用户自行签名构建的 iOS App 完全一致；VoIP 主题会自动添加 .voip。'));
		o.rmempty = true;
		o.validate = function(section_id, value) {
			return !value || /^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/.test(value) || _('请输入有效的 Bundle ID');
		};

		o = s.option(form.DummyValue, '_apns_key', _('APNs 私钥'),
			_('选择 Apple Developer 下载的 .p8 文件。密钥仅上传至本节点的 root 专用文件，不会回显。请通过可信的本地网络或 HTTPS 打开 LuCI。'));
		o.renderWidget = function() {
			var input = E('input', { 'type': 'file', 'accept': '.p8', 'style': 'max-width:260px' });
			var button = E('button', { 'type': 'button', 'class': 'cbi-button cbi-button-apply',
				'click': function() {
					var file = input.files && input.files[0];
					if (!file || file.size > 16384) {
						ui.addNotification(null, E('p', _('请选择不超过 16 KB 的 APNs .p8 文件。')), 'error');
						return;
					}
					return file.text().then(function(contents) {
						return fs.exec_direct('/usr/bin/sim-anywhere-push-key', [ 'prepare' ])
							.then(function() { return fs.write('/etc/sim-anywhere-apns-key.p8.pending', contents, 384); })
							.then(function() { return fs.exec_direct('/usr/bin/sim-anywhere-push-key', [ 'activate' ]); });
					}).then(function() {
						ui.addNotification(null, E('p', _('APNs 密钥已保存。')), 'info');
						window.location.reload();
					}, function() {
						ui.addNotification(null, E('p', _('密钥保存失败。请检查 .p8 文件。')), 'error');
					});
				}
			}, _('上传密钥'));
			return E('div', {}, [
				E('div', {}, String(data[4]).trim() == 'configured' ? _('已配置') : _('未配置')),
				E('div', { 'style': 'display:flex;gap:8px;align-items:center;margin-top:6px' }, [ input, button ])
			]);
		};

		s = m.section(form.NamedSection, 'quota', 'quota', _('流量控制'),
			_('用量达到套餐流量的设定比例或每日上限时断开移动数据。注意断开后 Wi-Fi Calling 将不可用。'));

		o = s.option(form.DummyValue, '_quota_state', _('当前状态'));
		o.cfgvalue = function() {
			if (!qs)
				return _('未知（sim-anywhere-quota 未运行）');
			var ratio = qs.ratio > 0 ? qs.ratio : 100;
			var text = {
				limit: ratio < 100
					? _('已达套餐流量的 %d%%，移动数据已断开').format(ratio)
					: _('已达套餐流量，移动数据已断开'),
				daily: _('已达每日上限，移动数据已断开')
			};
			return text[qs.block] || _('移动数据可用');
		};

		o = s.option(form.DummyValue, '_quota_usage', _('用量'));
		o.renderWidget = function() { return quotaPanel(qs); };

		o = s.option(form.Flag, 'cycle_limit', _('周期流量限制'),
			_('开启后，本周期用量达到套餐流量的设定比例即断开移动数据。与每日限制互不影响。'));
		o.default = '0';
		o.rmempty = false;

		o = sizeOption(s, 'plan', _('套餐流量'),
			_('周期内可用的流量，单位可选。留空或 0 表示不限制周期用量。'), 'GB');

		o = sliderOption(s, 'cycle_ratio', _('限制比例'),
			_('本周期用量达到套餐流量乘以该比例时断开移动数据。100 表示用满套餐流量。'), 1, 100, '%', 100);

	o = s.option(form.ListValue, 'cycle_mode', _('账单周期'),
		_('决定新一期的开始日：自然月为每月 1 日；指定日期为每月固定日期；固定天数以账单开始日为基准，每 N 天一期。'));
	o.value('month', _('自然月'));
	o.value('date', _('指定日期'));
	o.value('days', _('固定天数'));
	o.widget = 'radio';
	o.default = 'month';
	o.rmempty = false;

	o = sliderOption(s, 'reset_day', _('套餐重置日'),
		_('每月的第几日重置周期（1–28）。'), 1, 28, ' 日', 1);
	o.depends('cycle_mode', 'date');

	o = sliderOption(s, 'cycle_days', _('周期天数'),
		_('每 N 天为一个周期。'), 1, 365, ' 天', 30);
	o.depends('cycle_mode', 'days');

	o = s.option(form.Value, 'cycle_start', _('账单开始日'),
		_('固定天数周期的第一期开始日期（YYYY-MM-DD），必须早于今天。'));
	o.depends('cycle_mode', 'days');
	o.rmempty = false;
	o.renderWidget = function(section_id, option_index, cfgvalue) {
		var widget = new ui.Textfield(cfgvalue != null ? cfgvalue : this.default, {
			'id': this.cbid(section_id),
			'validate': this.getValidator(section_id),
			'disabled': this.readonly != null ? this.readonly : this.map.readonly
		}).render();
		widget.querySelector('input').type = 'date';
		return widget;
	};
	o.validate = function(section_id, value) {
		if (!/^\d{4}-\d{2}-\d{2}$/.test(value || ''))
			return _('请输入 YYYY-MM-DD 格式的日期');
		var d = new Date(value + 'T00:00:00');
		if (isNaN(d.getTime()))
			return _('日期无效');
		var today = new Date();
		today.setHours(0, 0, 0, 0);
		return d < today || _('账单开始日必须早于今天');
	};

		o = s.option(form.Flag, 'daily_limit', _('每日流量限制'),
			_('开启后，今日用量达到每日上限即断开移动数据。与周期限制互不影响。'));
		o.default = '0';
		o.rmempty = false;

		o = sizeOption(s, 'daily', _('每日流量上限'),
			_('按本机时间 0 点计日，单位可选。留空或 0 表示不限制。'), 'MB');

		s = m.section(form.NamedSection, 'discovery', 'discovery', _('节点发现'),
			_('启用后，本节点会定时向局域网发送自动发现信号（UDP 端口 8766），SIM Anywhere App 可以自动找到它，并一键完成连接。信号只包含节点名称、IP 和设备型号，不含状态或短信数据。'));

		o = s.option(form.DummyValue, '_discovery_state', _('当前状态'));
		o.cfgvalue = function() {
			return st && st.enabled && !st.stopped && !st.paused ? _('可发现') : _('不可发现');
		};

		o = s.option(form.Button, '_discovery_toggle', _('发现开关'));
		var broadcasting = !!(st && st.enabled && !st.stopped && !st.paused);
		o.inputtitle = broadcasting ? _('关闭发现') : _('允许发现');
		o.inputstyle = broadcasting ? 'reset' : 'apply';
		// One call into the daemon: it flips the flag file (effect within a second), writes
		// the UCI value that survives a reboot, and prints the new state. Going through the
		// helper instead of the uci RPC keeps the button from hanging half-way.
		o.onclick = function() {
			var turnOn = !broadcasting;
			return fs.exec_direct('/usr/bin/dongle-announce', [ turnOn ? 'start' : 'stop' ])
				.then(function() {
					ui.addNotification(null, E('p', turnOn ? _('已允许发现。') : _('已关闭发现。')), 'info');
					setTimeout(function() { window.location.reload(); }, 800);
				}, function(err) {
					ui.addNotification(null, E('p', _('操作失败：%s').format(err)), 'error');
				});
		};

		o = s.option(form.Flag, 'stop_on_connect', _('连接后关闭发现'),
			_('有 App 连上本节点后自动关闭发现。需要让其他设备发现本节点时，点上方“允许发现”；发现会持续到下一次有 App 连接。'));
		o.default = '0';
		o.rmempty = false;

	// Apply the layout again when LuCI redraws the map after save or reset.
	var renderContents = m.renderContents;
	m.renderContents = function() {
		return Promise.resolve(renderContents.apply(this, arguments)).then(function(root) {
			decorate(root);
			return root;
		});
	};
	return m.render();
}
});
