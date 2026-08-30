const test = require('node:test');
const assert = require('node:assert/strict');

const {
  availablePluginUpdates,
  platformPluginIds,
  platformUpdateCandidates
} = require('../wandao_electron/renderer/plugin_update_state.js');

test('filters incompatible and duplicate plugin updates', () => {
  const updates = availablePluginUpdates([
    { id: 'feishu', updateAvailable: true, compatibility: { compatible: true } },
    { id: 'feishu', updateAvailable: true, compatibility: { compatible: true } },
    { id: 'legacy', updateAvailable: true, compatibility: { compatible: false } },
    { id: 'idle', updateAvailable: false, compatibility: { compatible: true } },
    { id: 'ima', updateAvailable: true }
  ]);

  assert.deepEqual(updates.map((plugin) => plugin.id), ['feishu', 'ima']);
});

test('maps a platform only to the plugins that provide its actions', () => {
  const group = {
    providers: [
      { id: 'feishu-export', pluginId: 'feishu' },
      { id: 'feishu-import', pluginId: 'feishu' },
      { id: 'legacy-feishu-guide', pluginId: 'legacy' },
      { id: 'local-tool' }
    ]
  };
  const plugins = [
    { id: 'feishu', updateAvailable: true, compatibility: { compatible: true } },
    { id: 'legacy', updateAvailable: true, compatibility: { compatible: false } },
    { id: 'ima', updateAvailable: true, compatibility: { compatible: true } }
  ];

  assert.deepEqual(platformPluginIds(group), ['feishu', 'legacy']);
  assert.deepEqual(platformUpdateCandidates(group, plugins).map((plugin) => plugin.id), ['feishu']);
});
