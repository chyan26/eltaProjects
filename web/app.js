// Highlight Builder tool page. Talks to the local backend (docs/api/highlight-builder.openapi.yaml).
(() => {
  const API = "/api/v1";
  const HB = window.HB;
  const t = (en, zh) => HB.t(en, zh);
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const ZH = {
    "Strikeout": "三振", "Groundout": "滾地球出局", "Single": "一壘安打", "Flyout": "高飛球出局", "Walk": "保送",
    "Home Run": "全壘打", "Lineout": "平飛球出局", "Pop Out": "內野飛球出局", "Double": "二壘安打", "Triple": "三壘安打",
    "Strikeout Double Play": "三振雙殺", "Grounded Into DP": "滾地雙殺", "Double Play": "雙殺", "Forceout": "封殺",
    "Fielders Choice Out": "野手選擇出局", "Fielders Choice": "野手選擇", "Sac Fly": "高飛犧牲打", "Sac Bunt": "犧牲觸擊",
    "Caught Stealing 2B": "盜二壘失敗", "Caught Stealing 3B": "盜三壘失敗", "Hit By Pitch": "觸身球", "Intent Walk": "故意四壞",
    "Field Error": "失誤上壘", "Sac Fly Double Play": "犧牲飛球雙殺", "Pickoff 1B": "一壘牽制出局",
  };
  const TEAM_ZH = { CIN: "辛辛那提紅人", MIL: "密爾瓦基釀酒人", Reds: "紅人", Brewers: "釀酒人" };
  const GROUPS = {
    k: (e) => e.startsWith("Strikeout"),
    hits: (e) => ["Single", "Double", "Triple", "Home Run"].includes(e),
    outs: (e) => /out|Strikeout|DP|Double Play|Triple Play|Forceout|Sac |Caught Stealing|Pickoff/.test(e) && e !== "Walk",
    walk: (e) => e === "Walk" || e === "Intent Walk",
  };
  const typeName = (e) => t(e, ZH[e] || e);

  // ------------------------------------------------------------------ state
  const STORE = "hb-tool-v1";
  const defaults = {
    videoPath: "video/eltaMax10_Reds_Brewers_0913.mp4", gamePk: 823734, mode: "full", offset: 150.5,
    halfInning: 2, halfSide: "bottom", autoAnchor: true, anchorSeconds: "", audio: true,
    types: ["Strikeout", "Home Run"], teams: [], role: "bat", batters: [], pitchers: [],
    innFrom: 1, innTo: 9, scoring: false, official: false, pre: 8, post: 12, outDir: "", outEdited: false,
    overwrite: false, blur: false, blurMode: "blur", view: "multi", showCmds: false, lastJobId: null, lastRunVideo: "", lastRunScope: "",
  };
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { saved = {}; }
  const S = Object.assign({}, defaults, saved);
  ["types", "teams", "batters", "pitchers"].forEach((k) => (S[k] = new Set(S[k])));
  const R = { env: null, videos: [], game: null, preview: null, request: null, phase: "setup", stale: false, job: null, es: null, clips: [], sel: 0, edits: new Set(), trimUndo: {} };

  function persist() {
    const out = {};
    Object.keys(defaults).forEach((k) => (out[k] = S[k] instanceof Set ? [...S[k]] : S[k]));
    try { localStorage.setItem(STORE, JSON.stringify(out)); } catch (e) { /* storage blocked */ }
  }

  // ------------------------------------------------------------------ helpers
  async function api(method, path, body) {
    const res = await fetch(API + path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty */ }
    if (!res.ok) { const err = new Error((data && data.message) || res.statusText); err.status = res.status; err.body = data; throw err; }
    return data;
  }
  function toast(msg) {
    const el = $("#toast"); el.textContent = msg; el.classList.add("on");
    clearTimeout(toast.h); toast.h = setTimeout(() => el.classList.remove("on"), 1800);
  }
  function setSeg(sel, v) { $$(`${sel} button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v))); }
  const clipsN = (n) => t(`${n} clip${n === 1 ? "" : "s"}`, `${n} 段片段`);
  const teamOf = (side) => (R.game ? R.game.teams.find((x) => x.side === side) : null);
  const teamShort = (side) => {
    const team = teamOf(side);
    if (!team) return side === "away" ? t("Away", "客隊") : t("Home", "主隊");
    const short = team.name.split(" ").slice(-1)[0];
    return t(short, TEAM_ZH[short] || TEAM_ZH[team.abbreviation] || team.name);
  };
  const slug = (s) => s.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");

  // ------------------------------------------------------------------ filters
  const battingSide = (half) => (half === "top" ? "away" : "home");
  function matches(ignoreTypes = false) {
    if (!R.game) return [];
    return R.game.plays.filter((p) => {
      if (S.mode === "half" && (p.inning !== Number(S.halfInning) || p.half !== S.halfSide)) return false;
      if (!ignoreTypes && S.types.size && !S.types.has(p.event_type)) return false;
      if (S.teams.size) {
        const side = S.role === "bat" ? battingSide(p.half) : (battingSide(p.half) === "away" ? "home" : "away");
        if (!S.teams.has(side)) return false;
      }
      if (S.batters.size || S.pitchers.size) {
        if (!(S.batters.has(p.batter) || S.pitchers.has(p.pitcher))) return false;
      }
      if (p.inning < S.innFrom || p.inning > S.innTo) return false;
      if (S.scoring && !p.is_scoring_play) return false;
      if (S.official && p.official_highlight === false) return false;
      return true;
    });
  }

  function otherFilters() {
    const parts = [];
    if (S.mode === "half") parts.push(t(`inning ${S.halfInning} ${S.halfSide}`, `第 ${S.halfInning} 局${S.halfSide === "top" ? "上" : "下"}`));
    if (S.teams.size) parts.push([...S.teams].map(teamShort).join(" + ") + (S.role === "bat" ? t(" batting", " 打擊") : t(" pitching", " 投球")));
    const people = [...S.batters, ...S.pitchers];
    if (people.length) parts.push(people.length > 2 ? t(`${people.length} players`, `${people.length} 位球員`) : people.join(", "));
    if (S.innFrom !== 1 || S.innTo !== 9) parts.push(t(`innings ${S.innFrom}–${S.innTo}`, `第 ${S.innFrom}–${S.innTo} 局`));
    if (S.scoring) parts.push(t("scoring only", "只看得分"));
    if (S.official) parts.push(t("official highlights only", "只看官方精華"));
    return parts;
  }

  function summaryParts(n) {
    const parts = [];
    if (S.teams.size) parts.push([...S.teams].map(teamShort).join(" + ") + (S.role === "bat" ? t(" batting", " 打擊") : t(" pitching", " 投球")));
    const people = [...S.batters, ...S.pitchers];
    if (people.length) parts.push(people.join(", "));
    parts.push(S.types.size ? [...S.types].map(typeName).join(t(", ", "、")) : t("All play types", "所有事件類型"));
    if (S.innFrom !== 1 || S.innTo !== 9) parts.push(t(`Innings ${S.innFrom}–${S.innTo}`, `第 ${S.innFrom}–${S.innTo} 局`));
    if (S.scoring) parts.push(t("Scoring only", "只看得分"));
    if (S.official) parts.push(t("Official highlights only", "只看官方精華"));
    parts.push(S.mode === "full" ? t("Full game", "整場比賽") : t(`Inning ${S.halfInning} ${S.halfSide}`, `第 ${S.halfInning} 局${S.halfSide === "top" ? "上" : "下"}`));
    parts.push(t(`${S.pre} s before, ${S.post} s after`, `事件前 ${S.pre} 秒、後 ${S.post} 秒`));
    if (n !== undefined) parts.push(clipsN(n));
    return parts;
  }

  // One folder (and one play list) per game + video, not per filter: clips cut for one set of
  // filters are reused when the filters change, instead of being cut again into a new folder.
  function videoKey() {
    const path = S.videoPath || "";
    const stem = path.split("/").pop().replace(/\.[^.]+$/, "");
    let key = slug(stem) || "video";
    if (/[^\x00-\x7F]/.test(stem)) {  // non-ASCII names lose letters in slug(); add a short hash to stay unique
      let h = 0;
      for (const ch of path) h = (h * 31 + ch.codePointAt(0)) >>> 0;
      key += "_" + h.toString(36).slice(0, 6);
    }
    return key;
  }
  const videoName = (path) => (path || "").split("/").pop();
  const scopeText = () => (S.mode === "full" ? t("full game", "整場比賽") : t(`inning ${S.halfInning} ${S.halfSide}`, `第 ${S.halfInning} 局${S.halfSide === "top" ? "上" : "下"}`));
  function folderName() {
    return `sport_hightlight/dataset/${S.gamePk}_${videoKey()}`;
  }

  // ------------------------------------------------------------------ request (JobCreateRequest)
  function buildRequest(requestId) {
    const csv = `sport_hightlight/csv_data/${S.gamePk}_${videoKey()}_ground_truth.csv`;
    const a1 = { game_pk: Number(S.gamePk), video: S.videoPath };
    if (S.mode === "full") a1.game_offset_seconds = Number(S.offset);
    else {
      a1.clip_inning = Number(S.halfInning); a1.clip_half = S.halfSide;
      if (S.autoAnchor) a1.auto_anchor = true; else a1.anchor_video_seconds = Number(S.anchorSeconds);
    }
    if (S.audio) a1.audio_calibrate = true;
    a1.out = csv;
    const a2 = { ground_truth: csv, video: S.videoPath, out_dir: S.outDir, pre_seconds: S.pre, post_seconds: S.post, overwrite: S.overwrite };
    if (S.types.size) a2.event_types = [...S.types];
    if (S.teams.size) a2[S.role === "bat" ? "batting_team" : "pitching_team"] = [...S.teams];
    if (S.batters.size) a2.batters = [...S.batters];
    if (S.pitchers.size) a2.pitchers = [...S.pitchers];
    if (S.innFrom !== 1 || S.innTo !== 9) a2.innings = { from: S.innFrom, to: S.innTo };
    if (S.scoring) a2.scoring_only = true;
    if (S.official) a2.official_only = true;
    const steps = [
      { id: "find_plays", tool: "mlb_highlight_groundtruth", depends_on: [], args: a1 },
      { id: "cut_clips", tool: "make_clip", depends_on: ["find_plays"], args: a2 },
    ];
    if (S.blur) steps.push({ id: "blur_flags", tool: "mask_flags", depends_on: ["cut_clips"], for_each: { clips_from: "cut_clips", output_suffix: "_masked" }, args: { mode: S.blurMode } });
    const n = matches().length;
    return { request_id: requestId, display: { summary: summaryParts().join(" · "), expected_clip_count: n }, steps, on_failure: "stop", keep_finished_clips: true };
  }
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => { const r = (Math.random() * 16) | 0; return (c === "x" ? r : (r & 3) | 8).toString(16); }));

  // ------------------------------------------------------------------ render: setup
  function renderVideos() {
    const list = $("#videoList");
    if (!R.videos.length) { list.innerHTML = `<p class="empty">${t("No videos in the project's video/ folder yet.", "專案的 video/ 資料夾中還沒有影片。")}</p>`; return; }
    list.innerHTML = R.videos.map((v) => {
      const mb = v.size_bytes > 1e6 ? `${(v.size_bytes / 1e9).toFixed(2)} GB` : `${v.size_bytes} B`;
      const meta = v.lfs_placeholder ? `<span class="meta warn">${t("Git LFS placeholder", "Git LFS 佔位檔")}</span>` : `<span class="meta">${mb}${v.duration_seconds ? " · " + Math.round(v.duration_seconds / 60) + " min" : ""}</span>`;
      return `<button type="button" class="video-item" role="radio" aria-checked="${v.path === S.videoPath}" data-video="${esc(v.path)}"><span class="dot"></span><span class="name">${esc(v.path)}</span>${meta}</button>`;
    }).join("");
    const known = R.videos.find((v) => v.path === S.videoPath);
    $("#videoHint").textContent = known ? "" : t("Path not in the list; that's fine if you'll add the file later.", "路徑不在清單中；之後再放入檔案也可以。");
  }

  function renderGameStatus(state, msg) {
    const el = $("#gameStatus");
    if (state === "loading") el.innerHTML = `<span class="muted">${t("Loading game…", "正在載入比賽…")}</span>`;
    else if (state === "error") el.innerHTML = `<span class="err">${esc(msg)}</span>`;
    else if (R.game) {
      const g = R.game, away = teamOf("away"), home = teamOf("home");
      const src = g.source === "local_csv" ? t("saved play list (offline)", "已存的事件清單（離線）") : t("MLB API", "MLB API");
      el.innerHTML = `<span class="ok">✓ ${esc(teamShort("away"))} @ ${esc(teamShort("home"))}${g.date ? " · " + esc(g.date) : ""} · ${t(`${g.plate_appearances} plate appearances`, `${g.plate_appearances} 個打席`)}</span><small>${src}</small>`;
      void away; void home;
    } else el.innerHTML = "";
  }

  function renderSetup() {
    $("#videoPath").value = S.videoPath;
    $("#gamePk").value = S.gamePk;
    setSeg("#modeSeg", S.mode);
    $("#fullFields").hidden = S.mode !== "full";
    $("#halfFields").hidden = S.mode !== "half";
    $("#offset").value = S.offset;
    $("#halfInning").innerHTML = Array.from({ length: 12 }, (_, i) => `<option value="${i + 1}">${t(`Inning ${i + 1}`, `第 ${i + 1} 局`)}</option>`).join("");
    $("#halfInning").value = S.halfInning;
    $("#halfSide").value = S.halfSide;
    $("#autoAnchor").checked = S.autoAnchor;
    $("#anchorSeconds").hidden = S.autoAnchor;
    $("#anchorSeconds").value = S.anchorSeconds;
    $("#audio").checked = S.audio;
    renderVideos();
    renderGameStatus();
  }

  // ------------------------------------------------------------------ render: plays
  const tick = '<span class="tick"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#06080c" stroke-width="4" aria-hidden="true"><path d="m5 12 5 5 9-10" pathLength="1"/></svg></span>';
  function renderPlays() {
    const g = R.game;
    if (g) {
      // Chip numbers: plays of that type under every *other* filter (team, player, inning…).
      const pool = matches(true), byType = {};
      pool.forEach((p) => (byType[p.event_type] = (byType[p.event_type] || 0) + 1));
      const filtered = otherFilters().length > 0;
      $("#typeChips").innerHTML = g.play_types.map(({ event_type: e, count }) => {
        const main = HB.lang === "zh" ? (ZH[e] || e) : e, sub = HB.lang === "zh" ? e : (ZH[e] || "");
        const n = byType[e] || 0;
        const tip = filtered ? t(`${n} with your other filters · ${count} in the whole game`, `套用其他篩選後 ${n} 個 · 整場共 ${count} 個`) : t(`${count} in this game`, `本場共 ${count} 個`);
        return `<button type="button" class="chip${n === 0 ? " none" : ""}" data-type="${esc(e)}" aria-pressed="${S.types.has(e)}" title="${esc(tip)}">${tick}<span>${esc(main)}</span>${sub && sub !== main ? `<span class="sub">${esc(sub)}</span>` : ""}<span class="n">${n}${filtered && n !== count ? `<small>/${count}</small>` : ""}</span></button>`;
      }).join("");
      $("#teams").innerHTML = ["away", "home"].map((side) => `<button type="button" class="team" data-team="${side}" aria-pressed="${S.teams.has(side)}"><span class="side">${side === "away" ? t("Away · bats top", "客隊 · 上半局打擊") : t("Home · bats bottom", "主隊 · 下半局打擊")}</span><span class="tn">${esc(teamShort(side))}</span></button>`).join("");
      const q = $("#playerSearch").value.trim().toLowerCase();
      const groups = ["away", "home"].map((side) => {
        const rows = g.players.filter((p) => p.side === side && (!q || p.full_name.toLowerCase().includes(q)));
        if (!rows.length) return "";
        return `<div class="player-group">${esc(teamShort(side))}</div>` + rows.map((p) => {
          const isB = p.roles.includes("batter"), isP = p.roles.includes("pitcher");
          const picked = S.batters.has(p.full_name) || S.pitchers.has(p.full_name);
          const roles = [isB ? t("batter", "打者") : "", isP ? t("pitcher", "投手") : ""].filter(Boolean).join(" · ");
          return `<button type="button" class="prow" data-player="${esc(p.full_name)}" data-role="${isP && !isB ? "pitcher" : "batter"}" aria-pressed="${picked}"><span>${esc(p.full_name)}</span><span class="roles">${roles}</span></button>`;
        }).join("");
      }).join("");
      $("#playerList").innerHTML = groups || `<p class="empty">${t("No player matches that name.", "找不到這個名字的球員。")}</p>`;
    } else {
      $("#teams").innerHTML = ["away", "home"].map((side) => `<button type="button" class="team" data-team="${side}" aria-pressed="${S.teams.has(side)}"><span class="side">${side === "away" ? t("Away", "客隊") : t("Home", "主隊")}</span><span class="tn">—</span></button>`).join("");
    }
    $("#picked").innerHTML = [...S.batters, ...S.pitchers].map((n) => `<span class="chip">${esc(n)}<button class="x" type="button" data-unpick="${esc(n)}" aria-label="${t("Remove", "移除")} ${esc(n)}">×</button></span>`).join("");
    setSeg("#roleSeg", S.role);
    const opts = Array.from({ length: 9 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("");
    $("#innFrom").innerHTML = opts; $("#innTo").innerHTML = opts;
    $("#innFrom").value = S.innFrom; $("#innTo").value = S.innTo;
    $("#scoringOnly").checked = S.scoring; $("#officialOnly").checked = S.official;
    $("#pre").textContent = `${S.pre} s`; $("#post").textContent = `${S.post} s`;
    $("#total").textContent = `${S.pre + S.post} s`;
    if (!S.outEdited) S.outDir = folderName();
    $("#outDir").value = S.outDir;
    $("#overwrite").checked = S.overwrite; $("#blur").checked = S.blur;
    $("#blurSeg").hidden = !S.blur; setSeg("#blurSeg", S.blurMode);
    renderCount();
  }

  let lastCount = null;
  function renderCount() {
    const el = $("#count");
    if (!R.game) { el.textContent = "–"; $("#countOf").textContent = t("Load a game to count plays.", "載入比賽後才能計數。"); return; }
    const n = matches().length;
    if (n !== lastCount) { el.textContent = n; el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); lastCount = n; }
    el.classList.toggle("zero", n === 0);
    const total = S.mode === "half" ? R.game.plays.filter((p) => p.inning === Number(S.halfInning) && p.half === S.halfSide).length : R.game.plate_appearances;
    const others = otherFilters();
    const typesText = S.types.size ? t(`${S.types.size} play types`, `${S.types.size} 種事件類型`) : t("all play types", "所有事件類型");
    const base = n === 0 ? t("Nothing matches. Try removing a filter.", "沒有符合的事件，試著移除一個篩選條件。") : t(`of ${total} in ${S.mode === "half" ? "this half-inning" : "this game"}`, `${S.mode === "half" ? "本半局" : "本場"}共 ${total} 個`);
    $("#countOf").textContent = base + " · " + typesText + (others.length ? t(` · also filtering: ${others.join(", ")}`, ` · 另外套用：${others.join("、")}`) : "");
  }

  // ------------------------------------------------------------------ render: commands
  function fmtCommand(argv, layout) {
    const q = (s) => (/^[\w/.\-<>]+$/.test(s) ? s : `"${s.replace(/(["\\$`])/g, "\\$1")}"`);
    const parts = [];
    let i = 0;
    parts.push(esc(argv[0] + " " + argv[1]));
    i = 2;
    while (i < argv.length) {
      const a = argv[i];
      if (a.startsWith("--")) {
        const vals = [];
        let j = i + 1;
        while (j < argv.length && !argv[j].startsWith("--")) { vals.push(argv[j]); j++; }
        parts.push(`<span class="f">${esc(a)}</span>${vals.length ? " " + vals.map((v) => `<span class="v">${esc(q(v))}</span>`).join(" ") : ""}`);
        i = j;
      } else { parts.push(`<span class="v">${esc(q(a))}</span>`); i++; }
    }
    return layout === "one" ? parts.join(" ") : parts.join(" \\\n  ");
  }
  function hlJson(txt) {
    return esc(txt).replace(/(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b/g, (m, str, colon, lit) => (str ? (colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`) : `<span class="b">${lit}</span>`));
  }
  const STEP_TITLES = { find_plays: () => t("Find the plays in the video", "找出影片中的事件"), cut_clips: () => t("Cut the clips", "剪出片段"), blur_flags: () => t("Blur flags", "模糊旗幟") };
  const copyIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';
  function renderCommands() {
    // Collapsed by default: most users never need to read the commands.
    $("#cmdsBody").hidden = !S.showCmds;
    $("#cmdsCollapsed").hidden = S.showCmds;
    $("#cmdsToggle").setAttribute("aria-expanded", String(S.showCmds));
    $("#cmdsToggleLabel").textContent = S.showCmds ? t("Hide commands", "隱藏指令") : t("Show commands", "顯示指令");
    const dot = $("#cmdsDot");
    dot.classList.toggle("ready", !!R.preview && !R.stale);
    dot.classList.toggle("stale", !!R.preview && R.stale);
    $("#cmdsCollapsedText").innerHTML = !R.preview
      ? esc(t("Press “Generate commands” below. You can run right after; no need to read them.", "按下方的「產生指令」，產生後就能直接執行，不需要看懂指令。"))
      : R.stale
        ? esc(t("Your choices changed. Press “Generate commands” again before running.", "選擇已變更，請再按一次「產生指令」後再執行。"))
        : `<b>${esc(t(`Ready: ${R.preview.length} steps.`, `已就緒：共 ${R.preview.length} 個步驟。`))}</b> ${esc(t(`${videoName(S.videoPath)} · ${scopeText()} · about ${clipsN(R.request.display.expected_clip_count)}.`, `${videoName(S.videoPath)} · ${scopeText()} · 預計 ${clipsN(R.request.display.expected_clip_count)}。`))} ${esc(t("Generating doesn't cut anything yet: press Run below to make the clips.", "產生指令還不會剪片，請按下方的「執行」開始產生片段。"))}`;
    setSeg("#viewSeg", S.view);
    $("#stale").hidden = !R.stale;
    $("#copyAll").disabled = !R.preview;
    if (!R.preview) { $("#termSum").textContent = t("Press “Generate commands” below to see them here.", "按下方的「產生指令」後會顯示在這裡。"); $("#cmds").innerHTML = ""; return; }
    $("#termSum").textContent = summaryParts(R.request.display.expected_clip_count).join(" · ");
    if (S.view === "json") {
      const txt = JSON.stringify(R.request, null, 2);
      $("#cmds").innerHTML = `<div class="cmd"><div><div class="t"><b>POST</b>${API}/jobs · create_job · ${t("sent when you press Run", "按下執行時送出")}</div><pre>${hlJson(txt)}</pre></div><button class="btn ghost small" type="button" data-copy="json" aria-label="${t("Copy", "複製")}">${copyIcon}</button></div>`;
      return;
    }
    $("#cmds").innerHTML = R.preview.map((c, i) => `<div class="cmd"><div><div class="t"><b>${i + 1}</b>${esc((STEP_TITLES[c.step_id] || (() => c.step_id))())}</div><pre>${fmtCommand(c.argv, S.view)}</pre>${c.note ? `<div class="note">${t("Runs once for each clip made in step 2.", "會針對步驟 2 產生的每段片段各執行一次。")}</div>` : ""}</div><button class="btn ghost small" type="button" data-copy="${i}" aria-label="${t("Copy step", "複製步驟")} ${i + 1}">${copyIcon}</button></div>`).join("");
  }
  function plainCommands() { return R.preview.map((c) => c.shell).join("\n\n"); }

  // ------------------------------------------------------------------ render: run
  function renderRun() {
    const job = R.job;
    $("#runHint").textContent = R.phase === "ready" ? t("Commands are ready. Press Run in the bar below.", "指令已就緒，請按下方操作列的「執行」。") : R.phase === "running" ? t("Running. You can keep using the page or reload it.", "執行中，可以繼續使用頁面或重新整理。") : R.stale ? t("Your choices changed. Generate again before running.", "選擇已變更，請重新產生後再執行。") : !job ? t("Generate the commands first, then press Run.", "請先產生指令，再按「執行」。") : "";
    const stepsSrc = job ? job.steps : (R.request ? R.request.steps.map((s) => ({ id: s.id, status: "waiting", progress: null })) : []);
    const label = { waiting: t("Waiting", "等待中"), running: t("Running", "執行中"), succeeded: t("Done", "完成"), failed: t("Failed", "失敗"), skipped: t("Skipped", "略過"), cancelled: t("Stopped", "已停止") };
    $("#runSteps").innerHTML = stepsSrc.map((s) => {
      const p = s.progress && s.status === "running" ? ` · ${s.progress.done} / ${s.progress.total}` : "";
      return `<li class="${s.status}"><span class="ic"></span><span>${esc((STEP_TITLES[s.id] || (() => s.id))())}</span><span class="st">${label[s.status] || s.status}${p}</span></li>`;
    }).join("");
    let pct = 0, text = t("Not started", "尚未開始");
    if (job) {
      const n = job.steps.length;
      const doneSteps = job.steps.filter((s) => s.status === "succeeded").length;
      const running = job.steps.find((s) => s.status === "running");
      const frac = running && running.progress && running.progress.total ? running.progress.done / running.progress.total : 0;
      pct = Math.round(((doneSteps + frac) / n) * 100);
      if (job.status === "queued") text = t("Queued…", "排隊中…");
      else if (job.status === "running" && running) {
        if (running.id === "cut_clips" && running.progress) text = t(`Cutting clip ${running.progress.done} of ${running.progress.total}`, `正在剪第 ${running.progress.done} / ${running.progress.total} 段`);
        else if (running.id === "blur_flags" && running.progress) text = t(`Blurring flags · ${running.progress.done} of ${running.progress.total}`, `正在模糊旗幟 · ${running.progress.done} / ${running.progress.total}`);
        else text = (STEP_TITLES[running.id] || (() => running.id))() + "…";
      } else if (job.status === "succeeded") { const reusedN = job.outputs.reused_clips || 0; text = t(`Done. ${clipsN(job.outputs.clips.length)} ready${reusedN ? ` (${reusedN} reused from earlier runs)` : ""}.`, `完成，共 ${clipsN(job.outputs.clips.length)}${reusedN ? `（${reusedN} 段沿用之前的片段）` : ""}。`) + (job.demo ? t(" (demo: no files were written)", "（示範：沒有寫入檔案）") : ""); pct = 100; }
      else if (job.status === "cancelled") { const k = job.outputs.cancelled_after_clips || 0; text = t(`Stopped. ${clipsN(k)} finished before you stopped.`, `已停止，停止前完成了 ${clipsN(k)}。`); }
      else if (job.status === "failed") text = t("The run stopped with a problem.", "執行時發生問題而停止。");
    }
    $("#runLabel").textContent = text;
    $("#runBar").style.width = `${pct}%`;
    $("#runBar").parentElement.classList.toggle("fail", !!job && job.status === "failed");
    $("#stopBtn").hidden = !(job && (job.status === "running" || job.status === "queued"));
    const err = job && job.error;
    $("#runError").hidden = !err;
    if (err) {
      const stepName = err.step_id ? (STEP_TITLES[err.step_id] || (() => err.step_id))() : "";
      $("#runErrorTitle").textContent = stepName ? t(`Stopped at “${stepName}”.`, `停在「${stepName}」。`) : t("The run stopped.", "執行已停止。");
      $("#runErrorMsg").textContent = errorText(err) + (err.finished_clips ? " " + t(`${clipsN(err.finished_clips)} finished and were kept.`, `已完成並保留 ${clipsN(err.finished_clips)}。`) : "");
      $("#runErrorDetails").textContent = err.details || t("No extra details.", "沒有其他詳細資訊。");
    }
  }
  function errorText(err) {
    const zh = { 40401: "找不到影片檔，請在步驟 1 確認檔案位置。", 40402: "MLB API 找不到這個比賽編號。", 42201: "篩選條件沒有符合任何事件，請放寬條件。", 50001: "工具執行時發生錯誤，詳見詳細資訊。", 50002: "這台電腦沒有安裝 ffmpeg 或 ffprobe。" };
    return HB.lang === "zh" && zh[err.code] ? zh[err.code] + (err.code === 40401 && /LFS/.test(err.message) ? "（目前是 Git LFS 佔位檔，請先執行 git lfs pull）" : "") : err.message;
  }

  // ------------------------------------------------------------------ render: edit (demo)
  function renderEdit() {
    const has = R.clips.length > 0;
    $("#editor").hidden = !has;
    $("#editEmpty").hidden = has;
    $("#exportFromEdit").hidden = !has;
    // Warn when the clips shown aren't what the current settings would make.
    const otherVideo = has && S.lastRunVideo && S.lastRunVideo !== S.videoPath;
    const pending = has && (R.stale || R.phase === "ready" || otherVideo);
    $("#editStale").hidden = !pending;
    if (pending) {
      const from = S.lastRunVideo ? videoName(S.lastRunVideo) + (S.lastRunScope ? ` · ${S.lastRunScope}` : "") : t("the previous run", "上一次執行");
      $("#editStale").textContent = otherVideo
        ? t(`These clips come from ${from}, but you've switched to ${videoName(S.videoPath)}. Press Run below to make clips from the new video (generate the commands first if needed), or export these clips first.`, `這些片段來自 ${from}，但你已經換成 ${videoName(S.videoPath)}。按下方「執行」產生新影片的片段（必要時先產生指令），也可以先輸出這些片段。`)
        : R.phase === "ready"
          ? t(`These clips come from the previous run (${from}). New commands are ready: press Run below to replace them, or export these clips first.`, `這些片段來自上一次執行（${from}）。新的指令已就緒：按下方「執行」就會換成新的片段，也可以先輸出這些片段。`)
          : t(`These clips come from the previous run (${from}), and you've changed the settings since. Press “Generate commands” and run again to replace them, or export these clips first.`, `這些片段來自上一次執行（${from}），之後你改了設定。按「產生指令」再執行，就會換成新的片段；也可以先輸出這些片段。`);
    }
    if (!has) return;
    R.sel = Math.min(R.sel, R.clips.length - 1);
    $("#clipCount").textContent = clipsN(R.clips.length);
    $("#clipSource").textContent = S.lastRunVideo ? t(`from ${videoName(S.lastRunVideo)}`, `來自 ${videoName(S.lastRunVideo)}`) : "";
    $("#clipList").innerHTML = R.clips.map((c, i) => `<li class="clip-item${i === R.sel ? " sel" : ""}" draggable="true" data-i="${i}">
      <span class="grip" aria-hidden="true">⋮⋮</span>
      <span class="thumb"><span class="d">${(c.b - c.a).toFixed(1)}s</span></span>
      <span class="ci"><b>${esc(typeName(c.event_type))}${c.batter ? " · " + esc(c.batter) : ""}</b><span>${halfLabel(c)}</span></span>
      <span>${R.edits.has(c.key) ? `<span class="pill ed">${t("Edited", "已剪輯")}</span>` : c.official_highlight ? '<span class="pill mlb">MLB</span>' : ""}</span></li>`).join("");
    const c = R.clips[R.sel];
    $("#viewerTitle").textContent = `${typeName(c.event_type)}${c.batter ? " · " + c.batter : ""}`;
    $("#viewerSub").textContent = halfLabel(c, true) + (c.official_highlight ? t(" · Official MLB highlight", " · MLB 官方精華") : "");
    $("#viewerPath").textContent = c.path;
    loadPlayer(c);
    updateTc();
    ["#trimA", "#trimB"].forEach((s) => { $(s).max = c.duration; });
    $("#trimA").value = c.a; $("#trimB").value = c.b;
    $("#trimLen").textContent = `${(c.b - c.a).toFixed(1)} s`;
    $("#trimSel").style.left = `${(c.a / c.duration) * 100}%`;
    $("#trimSel").style.width = `${((c.b - c.a) / c.duration) * 100}%`;
    const mark = (S.pre - c.a) / Math.max(0.5, c.b - c.a);
    $("#playMark").style.left = `${mark * 100}%`;
    $("#playMark").style.display = S.pre >= c.a && S.pre <= c.b ? "block" : "none";
    $("#confirmRm").hidden = true; $("#removeBtn").hidden = false;
    renderExport();
  }
  // ------------------------------------------------------------------ player
  const player = () => $("#player");
  const fmtTime = (x) => `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(Math.floor(x % 60)).padStart(2, "0")}`;
  function updateTc() {
    const c = R.clips[R.sel]; if (!c) return;
    const v = player(), pos = v && !v.hidden && v.readyState ? Math.max(0, v.currentTime - c.a) : 0;
    $("#viewerTc").textContent = `${fmtTime(pos)} / ${fmtTime(c.b - c.a)}`;
  }
  function showPlayerMessage(c, reason) {
    const v = player();
    v.hidden = true; v.removeAttribute("src"); v.load();
    $("#playTrim").disabled = true;
    const msg = $("#playerMsg"); msg.hidden = false;
    if (reason === "demo") {
      $("#playerMsgTitle").textContent = t("Nothing to play in demo mode", "示範模式沒有影片可以播放");
      $("#playerMsgText").textContent = t("The demo run walks through the steps without cutting video, so this clip file was never made. Run for real (ffmpeg + the videos from git lfs pull) and the clip plays here.", "示範執行只走過步驟、不會真的剪影片，所以這段片段的檔案並不存在。實際執行（安裝 ffmpeg、用 git lfs pull 取得影片）後就能在這裡播放。");
    } else {
      $("#playerMsgTitle").textContent = t("This clip file can't be found", "找不到這段片段的檔案");
      $("#playerMsgText").textContent = t(`The run listed ${c.path}, but the file isn't there. It may have been moved or deleted.`, `執行結果列出 ${c.path}，但檔案不在那裡，可能已被移動或刪除。`);
    }
  }
  function loadPlayer(c) {
    const v = player();
    if (R.job && R.job.demo) return showPlayerMessage(c, "demo");
    const src = "/media/" + c.path.split("/").map(encodeURIComponent).join("/");
    $("#playerMsg").hidden = true; v.hidden = false; $("#playTrim").disabled = false;
    if (v.dataset.src !== src) {
      v.dataset.src = src;
      v.onerror = () => showPlayerMessage(c, "missing");
      v.src = src;
    } else if (v.readyState && (v.currentTime < c.a || v.currentTime > c.b)) {
      v.currentTime = c.a;
    }
  }
  function bindPlayer() {
    const v = player();
    v.addEventListener("loadedmetadata", () => { const c = R.clips[R.sel]; if (c) v.currentTime = c.a; updateTc(); });
    v.addEventListener("timeupdate", () => {
      const c = R.clips[R.sel]; if (!c) return;
      if (v.currentTime >= c.b) { v.pause(); v.currentTime = c.b; }
      updateTc();
    });
    v.addEventListener("play", () => { const c = R.clips[R.sel]; if (c && (v.currentTime < c.a || v.currentTime >= c.b - 0.05)) v.currentTime = c.a; });
  }

  // ------------------------------------------------------------------ export (highlight_editor)
  const DEFAULT_FPS = 30000 / 1001;
  const parseFps = (txt) => { if (!txt) return null; const [a, b] = String(txt).split("/").map(Number); return b ? a / b : a || null; };
  function outputPath() {
    const bits = [String(S.gamePk), ...[...S.types].slice(0, 3).map((x) => slug(x).replace(/_/g, ""))];
    return `out/reels/${bits.join("_")}_reel.mp4`;
  }
  function buildProject(overwrite = false) {
    return {
      version: 1,
      output: outputPath(),
      overwrite,
      settings: { width: null, height: null, fps: null, crf: 20, preset: "fast" },
      segments: R.clips.map((c, i) => {
        const fps = c.fps_value || DEFAULT_FPS;
        const seg = { id: String(i + 1), clip: c.path, start_frame: Math.round(c.a * fps) };
        const end = Math.round(c.b * fps);
        seg.end_frame = c.b >= c.duration || (c.frame_count && end >= c.frame_count) ? null : end;
        return seg;
      }),
    };
  }
  // Ask highlight_editor for each clip's real fps and frame count so trims convert exactly.
  async function enrichClips(job) {
    if (!job || job.demo || !job.outputs.manifest) return;
    let res;
    try {
      res = await api("POST", "/edit/clips", { manifests: [job.outputs.manifest] });
    } catch (e) {
      if (e.status === 404) res = { clips: [] };  // manifest moved or deleted: every clip is gone
      else return;  // keep the estimate; export validation will catch problems
    }
    try {
      const byPath = new Map(res.clips.map((c) => [c.clip, c]));
      // Clips that were moved or deleted since the run can't be played or exported; drop them.
      const missing = R.clips.filter((c) => !(byPath.get(c.path) || {}).exists);
      if (missing.length) {
        R.clips = R.clips.filter((c) => !missing.includes(c));
        R.sel = 0;
        if (!R.clips.length) {
          S.lastJobId = null; R.job = null; R.phase = R.preview ? "ready" : "setup";
          persist();
          toast(t("The clips from your last run are no longer on disk, so that run was cleared.", "上次執行的片段檔已不在磁碟上，已清除那次的結果。"));
          renderAll();
          return;
        }
        toast(t(`${clipsN(missing.length)} from your last run are no longer on disk and were removed from the list.`, `上次執行有 ${clipsN(missing.length)}已不在磁碟上，已從清單移除。`));
      }
      R.clips.forEach((c) => {
        const info = byPath.get(c.path);
        if (!info || !info.exists) return;
        const fps = parseFps(info.fps);
        if (!fps || !info.frame_count) return;
        const duration = info.frame_count / fps;
        const wasFull = c.b >= c.duration;
        Object.assign(c, { fps_value: fps, fps_text: info.fps, frame_count: info.frame_count, duration });
        if (wasFull) c.b = duration;
        c.a = Math.min(c.a, duration - 0.5);
      });
      renderEdit(); renderCta();
    } catch (e) { /* keep the estimate; export validation will catch problems */ }
  }

  const X = { render: null, es: null, lastProject: null, conflict: false };
  function segmentName(id) {
    const c = R.clips[Number(id) - 1];
    return c ? `${typeName(c.event_type)}${c.batter ? " · " + c.batter : ""}` : `#${id}`;
  }
  function editorErrorText(er) {
    const zh = { out_of_range: "裁切範圍超出片段長度", clip_not_found: "找不到片段檔案", invalid_media: "片段不是可讀的影片", output_exists: "輸出檔已存在", ffmpeg_not_found: "沒有安裝 ffmpeg", ffmpeg_failed: "ffmpeg 執行失敗", io_error: "檔案系統錯誤", invalid_value: "設定值不正確", missing_field: "缺少必要欄位", manifest_not_found: "找不到 manifest" };
    const where = er.segment_id ? `${segmentName(er.segment_id)}: ` : "";
    return where + (HB.lang === "zh" && zh[er.code] ? `${zh[er.code]}（${er.message}）` : er.message);
  }
  function renderExport() {
    const sec = $("#s-export");
    if (sec.hidden) return;
    const demo = R.job && R.job.demo;
    const project = buildProject(X.conflict);
    const secs = R.clips.reduce((sum, c) => sum + (c.b - c.a), 0);
    $("#exportDemoTag").hidden = !demo;
    $("#exportSum").textContent = t(`${clipsN(project.segments.length)} joined in list order · about ${fmtTime(secs)} · saved to ${project.output}`, `${clipsN(project.segments.length)}依清單順序合併 · 約 ${fmtTime(secs)} · 存到 ${project.output}`);
    $("#exportJson").innerHTML = hlJson(JSON.stringify(X.lastProject || project, null, 2));
    $("#exportNote").textContent = R.clips.every((c) => c.fps_value) ? t("Frames use each clip's real fps from highlight_editor. end_frame null means “to the end of the clip”.", "影格依 highlight_editor 讀到的每段實際 fps 換算。end_frame 為 null 代表「到片段結尾」。") : t("Frames assume 29.97 fps (clip files weren't readable). end_frame null means “to the end of the clip”.", "影格以 29.97 fps 估算（讀不到片段檔）。end_frame 為 null 代表「到片段結尾」。");
    const r = X.render;
    let label = demo ? t("A demo run makes no clip files, so there's nothing to export. Run for real to export.", "示範執行不會產生片段檔，所以沒有東西可輸出。實際執行後才能輸出。") : t("Ready to export.", "可以輸出。");
    let pct = 0;
    if (r) {
      const p = r.progress || { done: 0, total: 1 };
      if (r.status === "queued") label = t("Starting…", "準備中…");
      else if (r.status === "running" && r.phase === "concat") { label = t("Joining the clips…", "正在合併片段…"); pct = 92; }
      else if (r.status === "running") { label = t(`Processing clip ${Math.min(p.done + 1, p.total)} of ${p.total}${r.current_segment ? " · " + segmentName(r.current_segment) : ""}`, `正在處理第 ${Math.min(p.done + 1, p.total)} / ${p.total} 段${r.current_segment ? " · " + segmentName(r.current_segment) : ""}`); pct = Math.round((p.done / Math.max(1, p.total)) * 90); }
      else if (r.status === "succeeded") { label = t("Done. Your final video is ready.", "完成，最終影片已產生。"); pct = 100; }
      else if (r.status === "cancelled") label = t("Export cancelled. No file was written.", "已取消輸出，沒有寫入檔案。");
      else if (r.status === "failed") label = t("The export stopped with a problem.", "輸出時發生問題而停止。");
    }
    $("#exportLabel").textContent = label;
    $("#exportBar").style.width = `${pct}%`;
    $("#exportBar").parentElement.classList.toggle("fail", !!r && r.status === "failed");
    $("#exportCancel").hidden = !(r && (r.status === "running" || r.status === "queued"));
    const done = r && r.status === "succeeded" && r.result;
    $("#exportResult").hidden = !done;
    if (done) {
      const src = "/media/" + r.result.output.split("/").map(encodeURIComponent).join("/");
      const v = $("#reelPlayer");
      if (v.dataset.src !== src + r.id) { v.dataset.src = src + r.id; v.src = `${src}?v=${encodeURIComponent(r.id)}`; }
      $("#reelPath").textContent = r.result.output;
      $("#reelInfo").textContent = `${fmtTime(r.result.duration)} · ${r.result.settings.width}×${r.result.settings.height} · ${r.result.settings.fps} fps`;
      $("#reelChapters").innerHTML = r.result.segments.map((sg, i) => `<li><button type="button" data-chapter="${sg.output_start}"><b>${fmtTime(sg.output_start)}</b>${esc(segmentName(sg.id))}</button></li>`).join("");
    }
  }
  function showExportErrors(body) {
    const errs = (body && body.editor_errors) || [];
    const fieldErrs = body && body.errors && body.errors.output ? body.errors.output._errors : [];
    X.conflict = errs.some((e) => e.code === "output_exists");
    $("#exportError").hidden = false;
    $("#exportErrorTitle").textContent = X.conflict ? t("A file with this name already exists.", "已經有同名的輸出檔。") : t("The export can't start with these edits.", "這些剪輯設定無法輸出。");
    const items = [...errs.map(editorErrorText), ...fieldErrs.map((e) => e.message)];
    if (!items.length && body && body.message) items.push(body.message);
    $("#exportErrorList").innerHTML = items.map((x) => `<li>${esc(x)}</li>`).join("");
    $("#exportOverwriteRow").hidden = !X.conflict;
    const detail = errs.map((e) => e.detail).filter(Boolean).join("\n\n");
    $("#exportDetails").textContent = detail || t("No extra details.", "沒有其他詳細資訊。");
  }
  async function exportVideo(overwrite = false) {
    $("#s-export").hidden = false;
    $("#exportError").hidden = true; $("#exportDetails").hidden = true;
    setTimeout(() => document.getElementById("s-export").scrollIntoView({ behavior: "smooth", block: "start" }), 50);
    if (R.job && R.job.demo) { X.render = null; renderExport(); toast(t("Demo run: nothing to export.", "示範執行：沒有東西可以輸出。")); return; }
    const project = buildProject(overwrite);
    X.lastProject = project; X.conflict = false;
    try {
      X.render = await api("POST", "/renders", project);
      renderExport(); renderCta(); watchRender(X.render.id);
    } catch (e) {
      X.render = null; renderExport(); showExportErrors(e.body); renderCta();
    }
  }
  function watchRender(id) {
    if (X.es) X.es.close();
    const es = new EventSource(`${API}/renders/${id}/events`);
    X.es = es;
    const on = (ev) => {
      X.render = JSON.parse(ev.data);
      if (["succeeded", "failed", "cancelled"].includes(X.render.status)) {
        es.close(); X.es = null;
        if (X.render.status === "failed") showExportErrors(X.render.error);
        if (X.render.status === "succeeded") toast(t("Final video ready", "最終影片已完成"));
      }
      renderExport(); renderCta();
    };
    es.addEventListener("JOB_UPDATE", on);
    es.addEventListener("JOB_DONE", on);
  }
  async function cancelRender() {
    if (!X.render) return;
    try { X.render = await api("POST", `/renders/${X.render.id}/cancel`); } catch (e) { /* ignore */ }
    if (X.es) { X.es.close(); X.es = null; }
    renderExport(); renderCta();
  }

  function halfLabel(c, long) {
    const side = c.half === "top" ? "away" : "home";
    if (HB.lang === "zh") return `第 ${c.inning} 局${c.half === "top" ? "上" : "下"} · ${teamShort(side)} 打擊`;
    return `${long ? (c.half === "top" ? "Top of inning " : "Bottom of inning ") : (c.half === "top" ? "Top " : "Bottom ")}${c.inning} · ${teamShort(side)} batting`;
  }
  function loadClips(job) {
    R.clips = (job.outputs.clips || []).map((c, i) => ({ ...c, key: c.path || String(i), duration: c.duration_seconds || S.pre + S.post, a: 0, b: c.duration_seconds || S.pre + S.post }));
    R.sel = 0; R.edits.clear(); R.trimUndo = {};
  }

  // ------------------------------------------------------------------ CTA + steps
  function renderCta() {
    const n = R.game ? matches().length : 0;
    const b = $("#mainBtn");
    b.disabled = false; b.classList.remove("stop"); b.title = "";
    if (R.phase === "running") { b.textContent = t("Stop", "停止"); b.classList.add("stop"); }
    else if (R.phase === "ready") b.textContent = t(`Run · make ${clipsN(R.request.display.expected_clip_count)}`, `執行 · 產生 ${clipsN(R.request.display.expected_clip_count)}`);
    else if (R.phase === "done" && X.render && (X.render.status === "running" || X.render.status === "queued")) {
      b.textContent = t("Exporting…", "輸出中…"); b.disabled = true; b.title = t("Wait for the export or cancel it above", "請等待輸出完成，或在上方取消");
    }
    else if (R.phase === "done") {
      const secs = R.clips.reduce((sum, c) => sum + (c.b - c.a), 0);
      b.textContent = t(`Export final video · ${clipsN(R.clips.length)} · ${fmtTime(secs)}`, `輸出最終影片 · ${clipsN(R.clips.length)} · ${fmtTime(secs)}`);
      if (!R.clips.length) { b.disabled = true; b.title = t("No clips left to export", "沒有可輸出的片段"); }
    }
    else {
      b.textContent = t("Generate commands", "產生指令");
      const problem = !R.game ? t("Load a game first", "請先載入比賽") : n === 0 ? t("No plays match your choices", "沒有符合選擇的事件") : !S.videoPath ? t("Choose a video first", "請先選擇影片") : null;
      if (problem) { b.disabled = true; b.title = problem; }
    }
    const sum = R.game ? summaryParts() : [t("Load a game to start", "載入比賽後開始")];
    $("#ctaSum").innerHTML = (R.game ? `<b>${clipsN(n)}</b> · ` : "") + esc(sum.join(" · ")) + (b.disabled && b.title ? ` · <span style="color:var(--orange)">${esc(b.title)}</span>` : "");
  }
  function renderSteps() {
    const order = ["setup", "plays", "cmds", "run", "edit"];
    const reached = R.clips.length ? 4 : R.job ? 3 : R.preview ? 2 : R.game ? 1 : 0;
    $$("#steps a").forEach((a) => { const i = order.indexOf(a.dataset.step); a.classList.toggle("done", i < reached); a.classList.toggle("on", i === reached); });
  }
  function renderAll() {
    renderSetup(); renderPlays(); renderCommands(); renderRun(); renderEdit(); renderCta(); renderSteps();
    renderExport();
  }
  function changed() {
    // Any new choice after generating or running means the next step is generating again.
    // Also after a reload that restored a finished run (no commands generated in this session yet).
    if (R.phase !== "running" && (R.preview || R.phase === "done")) { R.stale = true; if (R.phase === "ready" || R.phase === "done") R.phase = "setup"; }
    persist(); renderPlays(); renderCommands(); renderRun(); renderEdit(); renderCta(); renderSteps();
  }

  // ------------------------------------------------------------------ actions
  async function loadGame() {
    const pk = String($("#gamePk").value).trim();
    if (!/^\d+$/.test(pk)) { $("#gamePk").classList.add("invalid"); setTimeout(() => $("#gamePk").classList.remove("invalid"), 400); renderGameStatus("error", t("Enter the game number as digits, like 823734.", "請輸入數字的比賽編號，例如 823734。")); return; }
    S.gamePk = Number(pk); persist();
    renderGameStatus("loading");
    try {
      R.game = await api("GET", `/games/${pk}`);
      const names = new Set(R.game.players.map((p) => p.full_name));
      [S.batters, S.pitchers].forEach((set) => [...set].forEach((n) => { if (!names.has(n)) set.delete(n); }));
      const types = new Set(R.game.play_types.map((x) => x.event_type));
      [...S.types].forEach((x) => { if (!types.has(x)) S.types.delete(x); });
      renderGameStatus();
    } catch (e) {
      R.game = null;
      renderGameStatus("error", e.status === 404 ? t("The MLB API has no game with this number.", "MLB API 找不到這個比賽編號。") : t("Couldn't load the game. Check the connection and try again.", "無法載入比賽，請檢查網路後再試一次。"));
    }
    changed();
  }

  async function generate() {
    $("#formErrors").hidden = true;
    const request = buildRequest(uuid());
    try {
      const res = await api("POST", "/jobs/preview", request);
      R.preview = res.commands; R.request = request; R.stale = false; R.phase = "ready"; R.job = null;
      renderAll();
      document.getElementById(S.showCmds ? "s-cmds" : "s-run").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      showFormErrors(e.body);
    }
  }
  function showFormErrors(body) {
    const items = [];
    const walk = (node, path) => {
      if (!node || typeof node !== "object") return;
      if (node._errors) node._errors.forEach((er) => items.push(`${path.join(".") || t("request", "請求")}: ${er.message}`));
      Object.keys(node).filter((k) => k !== "_errors").forEach((k) => walk(node[k], [...path, k]));
    };
    walk(body && body.errors, []);
    $("#formErrors").innerHTML = `<strong>${t("The backend rejected these settings.", "後端不接受這些設定。")}</strong>${items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : esc((body && body.message) || "")}`;
    $("#formErrors").hidden = false;
    document.getElementById("s-cmds").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function run(fresh) {
    if (fresh) R.request = { ...R.request, request_id: uuid() };
    try {
      R.job = await api("POST", "/jobs", R.request);
      S.lastJobId = R.job.id; persist();
      R.phase = "running"; R.clips = []; $("#s-export").hidden = true;
      S.lastRunVideo = S.videoPath; S.lastRunScope = scopeText(); persist();
      renderAll(); watch(R.job.id);
      document.getElementById("s-run").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { showFormErrors(e.body); }
  }

  function watch(id) {
    if (R.es) R.es.close();
    const es = new EventSource(`${API}/jobs/${id}/events`);
    R.es = es;
    const onJob = (ev) => {
      R.job = JSON.parse(ev.data);
      if (["succeeded", "failed", "cancelled"].includes(R.job.status)) {
        es.close(); R.es = null;
        R.phase = R.job.status === "succeeded" ? "done" : "setup";
        if (R.job.status === "succeeded") { loadClips(R.job); enrichClips(R.job); toast(t(`${clipsN(R.clips.length)} ready to edit`, `${clipsN(R.clips.length)}可以剪輯了`)); }
        if (R.job.status === "failed") R.phase = "ready";
        renderAll();
        if (R.job.status === "succeeded") setTimeout(() => document.getElementById("s-edit").scrollIntoView({ behavior: "smooth", block: "start" }), 400);
      } else { R.phase = "running"; renderRun(); renderCta(); renderSteps(); }
    };
    es.addEventListener("JOB_UPDATE", onJob);
    es.addEventListener("JOB_DONE", onJob);
    es.onerror = () => { /* the browser retries automatically */ };
  }

  async function stop() {
    if (!R.job) return;
    try { R.job = await api("POST", `/jobs/${R.job.id}/cancel`); } catch (e) { /* ignore */ }
    if (R.es) { R.es.close(); R.es = null; }
    R.phase = "ready"; renderAll();
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast(t("Copied", "已複製")); }
    catch (e) { toast(t("Select the text and copy it manually", "請選取文字後手動複製")); }
  }

  // ------------------------------------------------------------------ events
  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.video) {
      S.videoPath = b.dataset.video;
      // Half-inning recordings are named like ..._bottom2nd.mp4 / ..._top6th.mp4
      const m = S.videoPath.match(/_(top|bottom)(\d+)(?:st|nd|rd|th)\.[a-z0-9]+$/i);
      if (m) {
        S.mode = "half"; S.halfSide = m[1].toLowerCase(); S.halfInning = Number(m[2]); S.autoAnchor = true;
        let msg = t(`Set to the ${S.halfSide} of inning ${m[2]} from the file name.`, `已依檔名設定為第 ${m[2]} 局${S.halfSide === "top" ? "上" : "下"}。`);
        // Only one team bats in a half-inning; a team filter for the other side would match nothing.
        if (S.teams.size && R.game && !matches().length) {
          S.teams.clear();
          msg += t(" Team filter cleared: the other team doesn't bat in this half.", " 已清除隊伍篩選：這個半局只有另一隊打擊。");
        }
        toast(msg);
      }
      renderSetup(); changed();
    }
    else if (b.id === "loadGame") loadGame();
    else if (b.closest("#modeSeg")) { S.mode = b.dataset.v; renderSetup(); changed(); }
    else if (b.dataset.type) { S.types.has(b.dataset.type) ? S.types.delete(b.dataset.type) : S.types.add(b.dataset.type); changed(); }
    else if (b.dataset.group && R.game) { R.game.play_types.forEach(({ event_type: x }) => { if (GROUPS[b.dataset.group](x)) S.types.add(x); }); changed(); }
    else if (b.dataset.team) { S.teams.has(b.dataset.team) ? S.teams.delete(b.dataset.team) : S.teams.add(b.dataset.team); changed(); }
    else if (b.closest("#roleSeg")) { S.role = b.dataset.v; changed(); }
    else if (b.dataset.player) {
      const n = b.dataset.player, set = b.dataset.role === "pitcher" ? S.pitchers : S.batters;
      if (S.batters.has(n) || S.pitchers.has(n)) { S.batters.delete(n); S.pitchers.delete(n); } else set.add(n);
      changed();
    }
    else if (b.dataset.unpick) { S.batters.delete(b.dataset.unpick); S.pitchers.delete(b.dataset.unpick); changed(); }
    else if (b.dataset.clear) { const k = b.dataset.clear; if (k === "players") { S.batters.clear(); S.pitchers.clear(); } else S[k].clear(); changed(); }
    else if (b.id === "clearAll") { S.types.clear(); S.teams.clear(); S.batters.clear(); S.pitchers.clear(); S.innFrom = 1; S.innTo = 9; S.scoring = false; S.official = false; changed(); }
    else if (b.dataset.step) { const k = b.dataset.step; S[k] = Math.max(0, Math.min(60, S[k] + Number(b.dataset.d))); changed(); }
    else if (b.closest("#blurSeg")) { S.blurMode = b.dataset.v; changed(); }
    else if (b.closest("#viewSeg")) { S.view = b.dataset.v; persist(); renderCommands(); }
    else if (b.id === "copyAll") copyText(S.view === "json" ? JSON.stringify(R.request, null, 2) : plainCommands());
    else if (b.dataset.copy !== undefined) copyText(b.dataset.copy === "json" ? JSON.stringify(R.request, null, 2) : R.preview[Number(b.dataset.copy)].shell);
    else if (b.id === "mainBtn") {
      if (R.phase === "running") stop();
      else if (R.phase === "ready") run(false);
      else if (R.phase === "done") exportVideo();
      else generate();
    }
    else if (b.id === "stopBtn") stop();
    else if (b.id === "resetPage") {
      // Clears saved choices and the last run (this browser only). Keeps the language. Deletes no files.
      try { localStorage.removeItem(STORE); } catch (err) { /* storage blocked */ }
      if (R.es) R.es.close();
      if (X.es) X.es.close();
      location.reload();
    }
    else if (b.closest("#cmdsToggle")) { S.showCmds = !S.showCmds; persist(); renderCommands(); }
    else if (b.id === "copyProject") copyText(JSON.stringify(X.lastProject || buildProject(), null, 2));
    else if (b.id === "exportCancel") cancelRender();
    else if (b.id === "exportFromEdit") exportVideo();
    else if (b.id === "exportOverwrite") exportVideo(true);
    else if (b.id === "exportDetailsBtn") { const d = $("#exportDetails"); d.hidden = !d.hidden; }
    else if (b.closest("#projectToggle")) { const pt = $("#projectToggle"), open = pt.getAttribute("aria-expanded") !== "true"; pt.setAttribute("aria-expanded", String(open)); $("#projectBody").hidden = !open; }
    else if (b.dataset.chapter !== undefined) { const v = $("#reelPlayer"); v.currentTime = Number(b.dataset.chapter); v.play().catch(() => {}); }
    else if (b.id === "playTrim") { const v = player(), c = R.clips[R.sel]; if (c && !v.hidden) { v.currentTime = c.a; v.play().catch(() => {}); } }
    else if (b.id === "retryBtn") run(true);
    else if (b.id === "detailsBtn") { const d = $("#runErrorDetails"); d.hidden = !d.hidden; }
    else if (b.id === "removeBtn") { $("#confirmRm").hidden = false; b.hidden = true; }
    else if (b.id === "confirmNo") { $("#confirmRm").hidden = true; $("#removeBtn").hidden = false; }
    else if (b.id === "confirmYes") {
      const [gone] = R.clips.splice(R.sel, 1); const at = R.sel;
      R.edits.add("rm:" + gone.key); renderEdit(); renderCta();
      toast(t("Clip removed. Press Undo in the next few seconds to bring it back.", "已刪除片段。幾秒內按「復原」可以救回。"));
      R.lastRemoved = { clip: gone, at };
    }
    else if (b.id === "undoTrim") {
      const c = R.clips[R.sel];
      if (R.lastRemoved) { R.clips.splice(R.lastRemoved.at, 0, R.lastRemoved.clip); R.edits.delete("rm:" + R.lastRemoved.clip.key); R.sel = R.lastRemoved.at; R.lastRemoved = null; }
      else if (c) { c.a = 0; c.b = c.duration; R.edits.delete(c.key); }
      renderEdit(); renderCta();
    }
  });
  $("#clipList").addEventListener("click", (e) => { const li = e.target.closest(".clip-item"); if (li) { R.sel = Number(li.dataset.i); R.lastRemoved = null; renderEdit(); } });
  let dragFrom = null;
  $("#clipList").addEventListener("dragstart", (e) => { const li = e.target.closest(".clip-item"); if (!li) return; dragFrom = Number(li.dataset.i); li.classList.add("dragging"); try { e.dataTransfer.setData("text/plain", String(dragFrom)); } catch (err) { /* ignore */ } });
  $("#clipList").addEventListener("dragover", (e) => { e.preventDefault(); $$(".clip-item").forEach((x) => x.classList.remove("over")); const li = e.target.closest(".clip-item"); if (li) li.classList.add("over"); });
  $("#clipList").addEventListener("drop", (e) => {
    e.preventDefault(); const li = e.target.closest(".clip-item"); if (!li || dragFrom === null) return;
    const to = Number(li.dataset.i);
    if (to !== dragFrom) { const [m] = R.clips.splice(dragFrom, 1); R.clips.splice(to, 0, m); R.sel = to; R.edits.add("order"); }
    dragFrom = null; renderEdit(); renderCta();
  });
  $("#clipList").addEventListener("dragend", () => { dragFrom = null; $$(".clip-item").forEach((x) => x.classList.remove("dragging", "over")); });
  const setTrim = (a, b) => {
    const c = R.clips[R.sel]; if (!c) return;
    a = Math.max(0, Math.min(a, c.duration - 0.5)); b = Math.max(a + 0.5, Math.min(b, c.duration));
    const startMoved = a !== c.a;
    c.a = a; c.b = b; (a !== 0 || b !== c.duration) ? R.edits.add(c.key) : R.edits.delete(c.key);
    const v = player(); if (!v.hidden && v.readyState) { v.pause(); v.currentTime = startMoved ? a : Math.max(a, b - 1); }
    R.lastRemoved = null; renderEdit(); renderCta();
  };
  $("#trimA").addEventListener("input", (e) => setTrim(Number(e.target.value), R.clips[R.sel].b));
  $("#trimB").addEventListener("input", (e) => setTrim(R.clips[R.sel].a, Number(e.target.value)));

  const bindInput = (id, key, fn = (v) => v, rerender = changed) => $(id).addEventListener("input", (e) => { S[key] = fn(e.target.value); rerender(); });
  bindInput("#videoPath", "videoPath", (v) => v.trim(), () => { renderVideos(); changed(); });
  bindInput("#offset", "offset", (v) => v);
  bindInput("#anchorSeconds", "anchorSeconds", (v) => v);
  bindInput("#outDir", "outDir", (v) => { S.outEdited = true; return v.trim(); });
  $("#playerSearch").addEventListener("input", renderPlays);
  $("#gamePk").addEventListener("keydown", (e) => { if (e.key === "Enter") loadGame(); });
  $("#halfInning").addEventListener("change", (e) => { S.halfInning = Number(e.target.value); changed(); });
  $("#halfSide").addEventListener("change", (e) => { S.halfSide = e.target.value; changed(); });
  $("#autoAnchor").addEventListener("change", (e) => { S.autoAnchor = e.target.checked; $("#anchorSeconds").hidden = S.autoAnchor; changed(); });
  $("#audio").addEventListener("change", (e) => { S.audio = e.target.checked; changed(); });
  $("#innFrom").addEventListener("change", (e) => { S.innFrom = Number(e.target.value); if (S.innTo < S.innFrom) S.innTo = S.innFrom; changed(); });
  $("#innTo").addEventListener("change", (e) => { S.innTo = Number(e.target.value); if (S.innFrom > S.innTo) S.innFrom = S.innTo; changed(); });
  $("#scoringOnly").addEventListener("change", (e) => { S.scoring = e.target.checked; changed(); });
  $("#officialOnly").addEventListener("change", (e) => { S.official = e.target.checked; changed(); });
  $("#overwrite").addEventListener("change", (e) => { S.overwrite = e.target.checked; changed(); });
  $("#blur").addEventListener("change", (e) => { S.blur = e.target.checked; changed(); });
  $("#videoFile").addEventListener("change", (e) => { const f = e.target.files && e.target.files[0]; if (f) { S.videoPath = `video/${f.name}`; renderSetup(); changed(); toast(t(`Using video/${f.name}. Put the file there before running.`, `使用 video/${f.name}，執行前請把檔案放到這裡。`)); } });
  document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("#mainBtn").disabled) $("#mainBtn").click(); });
  window.addEventListener("hb-langchange", () => renderAll());
  bindPlayer();

  // ------------------------------------------------------------------ start
  async function start() {
    renderAll();
    try {
      R.env = await api("GET", "/environment");
      const pill = $("#modePill"); pill.hidden = false;
      pill.textContent = R.env.demo ? t("Demo", "示範") : t("Live", "實際執行");
      pill.classList.toggle("real", !R.env.demo);
      if (R.env.demo) {
        const reasons = [];
        if (!R.env.ffmpeg) reasons.push(t("ffmpeg isn't installed", "沒有安裝 ffmpeg"));
        if (!R.env.real_videos.length) reasons.push(t("the videos are Git LFS placeholders", "影片是 Git LFS 佔位檔"));
        $("#demoReasons").textContent = reasons.length ? reasons.join(t("; ", "；")) + t(".", "。") : "";
        $("#demoBanner").hidden = false;
      }
    } catch (e) { /* server too old or offline */ }
    try { R.videos = await api("GET", "/videos"); } catch (e) { R.videos = []; }
    renderVideos();
    await loadGame();
    if (S.lastJobId) {
      try {
        const job = await api("GET", `/jobs/${S.lastJobId}`);
        R.job = job;
        if (job.status === "running" || job.status === "queued") { R.phase = "running"; watch(job.id); }
        else if (job.status === "succeeded") { R.phase = "done"; loadClips(job); enrichClips(job); }
        renderAll();
      } catch (e) { S.lastJobId = null; persist(); }
    }
  }
  window.addEventListener("hb-langchange", function once() { window.removeEventListener("hb-langchange", once); start(); });
})();
