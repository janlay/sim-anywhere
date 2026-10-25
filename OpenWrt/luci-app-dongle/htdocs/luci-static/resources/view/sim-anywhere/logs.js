'use strict';
'require view';
'require fs';

var sources = [
	[ 'sim-anywhere-calld', _('语音通话服务日志') ],
	[ 'sim-anywhere-vowifid', _('Wi-Fi Calling 服务日志') ],
	[ 'vowifi-calls', _('Wi-Fi Calling 通话记录') ],
	[ 'calls', _('通话记录（模组路径）') ]
];

return view.extend({
	render: function() {
		var source = E('select', { 'class': 'cbi-input-select' }, sources.map(function(s) {
			return E('option', { 'value': s[0] }, s[1]);
		}));
		var lines = E('input', { 'class': 'cbi-input-text', 'type': 'number', 'min': 1, 'max': 2000, 'value': 200 });
		var output = E('pre', { 'style': 'white-space: pre-wrap; word-break: break-all' }, '');

		function load() {
			return fs.exec_direct('/usr/bin/dongle-status', [ 'logs', source.value, String(lines.value) ])
				.then(function(text) {
					output.textContent = text || _('（无日志）');
				})
				.catch(function(err) {
					output.textContent = err.message;
				});
		}

		source.addEventListener('change', load);
		lines.addEventListener('change', load);

		load();

		return E('div', {}, [
			E('div', { 'class': 'cbi-section' }, [
				source, ' ', lines, ' ',
				E('button', { 'class': 'btn cbi-button', 'click': load }, _('刷新'))
			]),
			output
		]);
	}
});
