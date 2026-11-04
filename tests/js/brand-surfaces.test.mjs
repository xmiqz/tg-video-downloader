import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (name) => readFileSync(new URL(name, ROOT), "utf8");

const SETUP_HTML = read("web/setup.html");
const SETUP_CSS = read("web/css/setup.css");
const LOGIN_JS = read("web/js/views/login.js");
const LOGIN_CSS = read("web/css/views/login.css");

function rule(css, selector) {
  const start = css.indexOf(selector + " {");
  if (start < 0) return "";
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return close < 0 ? "" : css.slice(open + 1, close);
}

test("setup uses the shared XIAN app icon instead of a text badge", () => {
  assert.match(SETUP_HTML,
    /<img\s+class="setup-logo"\s+src="\/assets\/xian-icon\.svg"/,
    "首次设置页必须使用同源新版图标");
  assert.doesNotMatch(SETUP_HTML, /class="setup-logo">TG</,
    "首次设置页不得回退到无品牌文字徽标");
  assert.match(rule(SETUP_CSS, ".setup-logo"), /object-fit:\s*contain/,
    "同源 SVG 图标需按原比例渲染");
});

test("login build label lives inside the dialog and includes the build field", () => {
  assert.match(LOGIN_JS, /import \{ buildLabel \} from "\.\.\/build-label\.js"/);
  assert.match(LOGIN_JS, /\$\.loginCard\.append\(\$\.buildFoot\)/,
    "版本信息必须挂在登录弹层卡内");
  assert.match(LOGIN_JS, /\$\.acctCard\.body\.append\(\$\.buildFootAcct\)/,
    "账号态弹层也要显示同源构建信息");
  assert.doesNotMatch(LOGIN_JS, /\[\$\.loginCard, \$\.acctCard\.el, \$\.buildFoot\]/,
    "版本信息不能再作为弹层外的孤立节点");
  const foot = rule(LOGIN_CSS, ".lg-build");
  assert.match(foot, /position:\s*static/, "构建信息不能固定在视口底部");
  assert.doesNotMatch(foot, /position:\s*fixed/, "构建信息不能脱离弹层");
  assert.match(foot, /border-top:/, "弹层底部构建信息应有分隔线");
});
