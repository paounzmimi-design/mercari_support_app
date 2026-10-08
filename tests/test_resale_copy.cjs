const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function check(available, fallback = false) {
  let handler, copied, selected = false;
  const status = {textContent: ''};
  const field = {value: '商品説明のテスト', focus() {}, select() {selected = true;}};
  const button = {dataset: {copy: 'description'}, nextElementSibling: status};
  const context = {
    document: {addEventListener(type, callback) {handler = callback;}, getElementById() {return field;}, execCommand() {return fallback;}},
    navigator: available ? {clipboard: {async writeText(text) {copied = text;}}} : {},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../resale/static/copy.js'), 'utf8'), context);
  await handler({target: {closest(selector) {return selector.includes("data-copy") ? button : null;}}});
  if (available) {
    assert.equal(copied, field.value);
    assert.equal(status.textContent.trim(), 'コピーしました');
  } else if (fallback) {
    assert.equal(status.textContent.trim(), "コピーしました");
  } else {
    assert.equal(selected, true);
    assert.match(status.textContent, /手動でコピー/);
  }
}

(async () => {
  await check(true);
  await check(false);
  await check(false, true);
  console.log('Copy handler: 3 checks passed (mock DOM, not browser testing)');
})().catch(error => {console.error(error); process.exitCode = 1;});
