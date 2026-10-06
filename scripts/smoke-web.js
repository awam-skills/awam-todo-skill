// 临时冒烟：用最小 DOM 桩执行 web/index.html 的内联脚本，确认渲染管线不报错。
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/g);
const code = m[m.length - 1].replace(/^<script>/, "").replace(/<\/script>$/, "");

const mockItems = [
  { id: "T-20261002-010", text: "推进 tdx-linker", status: "进行中", importance: "重要",
    urgent: "不紧急", is_urgent: false, note: "", blocker: "晚上刷手机", counter: "拿起手机前先做 10 分钟",
    workspace: "G:\\Projects\\Stock\\tdx\\tdx_warn", docs: [], links: [], tags: ["学习"],
    depends_on: [], parent: "", due: "2026-09-20", created: "2026-10-02 22:11", updated: null,
    from: null, date: "2026-10-02", archived: false, blocked: false,
    overdue_days: 16, due_in_days: -16, stall_days: null, projected_finish: null,
    pin: 0, pin_reason: "逾期 16 天，已滚入今日" },
  { id: "T-20261002-004", text: "持续维护 vibe-astock", status: "进行中", importance: "不重要",
    urgent: "不紧急", is_urgent: false, note: "", blocker: "", counter: "",
    workspace: "G:\\Projects\\Stock\\vibe-astock", docs: [], links: [], tags: [],
    depends_on: [], parent: "", due: null, created: "2026-10-02 21:31", updated: null,
    from: null, date: "2026-10-02", archived: false, blocked: false,
    overdue_days: null, due_in_days: null, stall_days: 4, projected_finish: null,
    pin: 1, pin_reason: "已 4 天未推进，滚入今日" },
  { id: "T-20261002-006", text: "收尾 TGDown", status: "结束", importance: "不重要",
    urgent: "不紧急", is_urgent: false, note: "", blocker: "", counter: "", workspace: "",
    docs: [], links: [], tags: [], depends_on: [], parent: "", due: null,
    created: "2026-10-02 21:31", updated: null, from: null, date: "2026-10-02",
    archived: false, blocked: false, overdue_days: null, due_in_days: null,
    stall_days: null, projected_finish: null, pin: null, pin_reason: "" },
];

const els = {};
function makeEl(id) {
  return els[id] || (els[id] = {
    id, innerHTML: "", textContent: "", value: "", style: {},
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    addEventListener(){}, focus(){}, appendChild(){}, querySelector(){ return null; },
    closest(){ return null; }, getAttribute(){ return null; }, setAttribute(){},
    offsetWidth: 160, offsetHeight: 118,
  });
}
global.document = {
  getElementById: makeEl,
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener(){},
  createElement: () => ({ style:{}, select(){}, value:"" }),
  body: { appendChild(){}, removeChild(){} },
};
global.window = { addEventListener(){}, innerWidth: 1440, innerHeight: 900 };
global.localStorage = { getItem: () => null, setItem(){}, removeItem(){} };
global.navigator = { sendBeacon(){}, clipboard: { writeText: async () => {} } };
global.Blob = function(){};
global.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({
    schema_version: 2,
    summary: { today: 2, overdue: 1 },
    week: { start: "2026-10-05", end: "2026-10-11", created: 0, done: 1 },
    defaults: { status: "进行中", importance: "不重要", urgent: "不紧急" },
    items: mockItems, revision: "abc",
  }),
});
global.getComputedStyle = () => ({ display: "none" });

let failed = false;
function check(name, cond, extra) {
  console.log((cond ? "  PASS  " : "  FAIL  ") + name + (cond ? "" : "  | " + (extra || "")));
  if (!cond) failed = true;
}

(async () => {
  try {
    eval(code + "\n;global.__render = render; global.__setState = function(s){ currentState = s; };");
  } catch (e) {
    console.log("  FAIL  脚本执行抛错 | " + e.message);
    process.exit(1);
  }
  await new Promise(r => setTimeout(r, 300));

  const list = els["list"] ? els["list"].innerHTML : "";
  const nav = els["navSide"] ? els["navSide"].innerHTML : "";
  check("列表渲染出卡片", (list.match(/class="card/g) || []).length === 3, list.slice(0, 200));
  check("逾期卡有红色置顶说明", /pin red/.test(list) && /逾期 16 天/.test(list));
  check("停滞卡有琥珀置顶说明", /pin grace/.test(list) && /已 4 天未推进/.test(list));
  check("逾期卡给出两个出口", /立即推进/.test(list) && /调整计划/.test(list));
  check("卡片展示预案", /障碍/.test(list) && /拿起手机前先做 10 分钟/.test(list));
  check("无推算时显示暂无推算", /暂无推算/.test(list));
  check("导航渲染含今日视图", /data-k="today"/.test(nav));
  check("默认视图是全部(active)", /class="navitem active" data-k="all"/.test(nav), nav.slice(0, 160));
  check("默认视图不是今日入口", !/navitem active" data-k="today"/.test(nav));
  check("周口径条已渲染", /周一为起点/.test(els["weekbar"].innerHTML || ""));
  check("底部新建入口文案", /新增待办（也可以点这里）/.test(els["newEntryBottom"].innerHTML || ""));
  check("图标为内联 svg", /<svg class="ic/.test(list));
  check("无 emoji 输出", !/[\u{1F300}-\u{1FAFF}]/u.test(list));

  // 切到「今日」视图
  global.__setState("today"); global.__render();
  const todayList = els["list"].innerHTML || "";
  check("今日视图只剩置顶项", (todayList.match(/class="card/g) || []).length === 2, todayList.slice(0, 120));
  check("今日视图有口径说明", /今天要处理 2 条/.test(todayList));

  global.__setState("overdue"); global.__render();
  check("逾期视图只剩逾期项", (els["list"].innerHTML.match(/class="card/g) || []).length === 1);

  console.log(failed ? "\n结果：存在失败项" : "\n结果：全部通过");
  process.exit(failed ? 1 : 0);
})();
