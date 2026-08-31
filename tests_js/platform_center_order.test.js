const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'wandao_electron', 'renderer', 'app.js'), 'utf8');

function sourceBetween(start, end) {
  const startIndex = appSource.indexOf(start);
  const endIndex = appSource.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source marker: ${start}`);
  assert.notEqual(endIndex, -1, `missing source marker: ${end}`);
  return appSource.slice(startIndex, endIndex);
}

const context = {};
vm.runInNewContext([
  sourceBetween('const PLATFORM_ORDER = [', 'const PLATFORM_META = {'),
  sourceBetween('function platformSortIndex(key) {', '\nfunction platformGroups() {'),
  'globalThis.__comparePlatformGroups = comparePlatformGroups;'
].join('\n'), context);

const comparePlatformGroups = context.__comparePlatformGroups;

test('Google Docs is always the final platform-center card', () => {
  const groups = [
    { key: 'google-docs', name: 'Google Docs' },
    { key: 'wps', name: 'WPS' },
    { key: 'feishu', name: '飞书' },
    { key: 'csdn', name: 'CSDN' },
    { key: 'notion', name: 'Notion' }
  ];

  const sorted = groups.slice().sort(comparePlatformGroups);

  assert.deepEqual(
    sorted.map((group) => group.key),
    ['feishu', 'notion', 'csdn', 'wps', 'google-docs']
  );
});

test('adding Google Docs does not change the relative order of other platform-center cards', () => {
  const otherGroups = [
    { key: 'zhihu', name: '知乎' },
    { key: 'yuque', name: '语雀' },
    { key: 'obsidian', name: 'Obsidian' },
    { key: 'youdao', name: '有道云笔记' }
  ];
  const baseline = otherGroups.slice().sort(comparePlatformGroups).map((group) => group.key);
  const withGoogle = [
    { key: 'google-docs', name: 'Google Docs' },
    ...otherGroups
  ].sort(comparePlatformGroups);

  assert.equal(withGoogle.at(-1).key, 'google-docs');
  assert.deepEqual(withGoogle.slice(0, -1).map((group) => group.key), baseline);
});
