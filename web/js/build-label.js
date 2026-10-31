/**
 * 登录页页脚的构建信息文案。
 *
 * 数据来自 /auth/status 的 version / build / project 三个字段（服务端由
 * TGDL_VERSION / TGDL_BUILD 注入）。缺值是开发态，统一显示 dev，
 * 绝不渲染空行或 undefined。
 */
export function buildLabel(status) {
  const version = String((status && status.version) || "").trim() || "dev";
  const build = String((status && status.build) || "").trim();
  if (version === "dev") {
    return build ? `dev · build ${build}` : "dev";
  }
  return build ? `${version} · build ${build}` : version;
}
