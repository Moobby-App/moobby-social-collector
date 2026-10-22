// Local browser collector. Persist selected post fields, never cookies or full responses.
const fs = require('node:fs');
const path = require('node:path');
const {selectText} = require('./text_extraction.cjs');

function candidates(value, output = []) {
  if (Array.isArray(value)) {
    for (const child of value) candidates(child, output);
  } else if (value && typeof value === 'object') {
    if (value.tweet_results?.result) output.push(value.tweet_results.result);
    for (const [key, child] of Object.entries(value)) {
      if (!['tweet_results', 'quoted_status_result', 'retweeted_status_result'].includes(key)) candidates(child, output);
    }
  }
  return output;
}

function normalize(result, start, end) {
  const tweet = result.legacy ? result : result.tweet;
  if (!tweet?.legacy) return null;
  const legacy = tweet.legacy;
  const user = tweet.core?.user_results?.result;
  const username = user?.core?.screen_name || user?.legacy?.screen_name;
  const id = String(tweet.rest_id || legacy.id_str || '');
  const published = Date.parse(legacy.created_at);
  if (!username || !/^\d+$/.test(id) || !Number.isFinite(published) || published < start || published >= end) return null;
  const selected = selectText(result);
  return {
    id_str: id, username, author_display_name: user?.core?.name || user?.legacy?.name || null,
    tweet_url: `https://x.com/${username}/status/${id}`,
    published_at: new Date(published).toISOString(), full_text: selected.full_text || '',
    legacy_full_text: selected.legacy_full_text, text_extraction_method: selected.text_extraction_method,
    text_completeness: selected.text_completeness, post_type: selected.post_type,
    conversation_id: legacy.conversation_id_str || null,
    in_reply_to_status_id: legacy.in_reply_to_status_id_str || null,
    in_reply_to_screen_name: legacy.in_reply_to_screen_name || null,
    quoted_status_id: legacy.quoted_status_id_str || null,
    related_urls: (legacy.entities?.urls || []).map(item => item.expanded_url || item.url).filter(Boolean),
    media: JSON.parse(selected.media_json), media_extraction_basis: selected.media_extraction_basis,
  };
}

async function main(planFile) {
  const {chromium} = require('./node_modules/playwright');
  const plan = JSON.parse(fs.readFileSync(planFile, 'utf8'));
  const folder = path.dirname(planFile);
  const root = path.resolve(__dirname, '../..');
  const start = Date.parse(plan.window_start), end = Date.parse(plan.window_end);
  const browser = await chromium.launch({headless: false});
  let partial = false;
  try {
    const context = await browser.newContext({storageState: path.join(root, 'playwright/.auth/x_reused_session.json')});
    for (const query of plan.queries) {
      console.log(`\nMencari: ${query.id} | ${query.query}`);
      const page = await context.newPage();
      const posts = new Map(), pending = new Set();
      let blocked = false, errors = 0, responses = 0, status = 'scroll_limit';
      const output = path.join(folder, query.id + '.json');
      function save() {
        const report = {query, status, parse_errors: errors, relevant_responses: responses,
          finished_at: new Date().toISOString(), posts: [...posts.values()]};
        fs.writeFileSync(output + '.tmp', JSON.stringify(report, null, 2));
        fs.renameSync(output + '.tmp', output);
      }
      save();
      page.on('response', response => {
        const url = new URL(response.url());
        if (!(url.hostname === 'x.com' || url.hostname.endsWith('.x.com'))) return;
        if (response.status() === 429) blocked = true;
        if (!url.pathname.includes('SearchTimeline')) return;
        if ([401, 403].includes(response.status())) blocked = true;
        if (!response.ok()) return;
        const task = (async () => {
          try {
            const body = await response.json();
            if (body.errors?.length) { errors++; return; }
            responses++;
            for (const result of candidates(body)) {
              const post = normalize(result, start, end);
              if (post) {
                // Preserve distinct versions without writing repeated identical payloads.
                const key = JSON.stringify(post);
                if (!posts.has(key)) posts.set(key, {...post, captured_at: new Date().toISOString()});
              }
            }
            save();
          } catch { errors++; }
        })();
        pending.add(task);
        task.finally(() => pending.delete(task));
      });
      try {
        const url = `https://x.com/search?q=${encodeURIComponent(query.query)}&src=typed_query&f=live`;
        await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 60000});
        await page.waitForTimeout(5000);
        let previous = 0, stale = 0;
        for (let round = 0; round < 120; round++) {
          await Promise.all([...pending]);
          if (blocked) { status = 'access_or_rate_limited'; break; }
          if (/\/login|\/onboarding\/|\/account\/access/.test(page.url())) { status = 'login_or_verification'; break; }
          if (posts.size >= plan.max_posts_per_query) { status = 'post_limit'; break; }
          stale = posts.size === previous ? stale + 1 : 0;
          previous = posts.size;
          if (errors) { status = 'response_error'; break; }
          if (stale >= 8) { status = responses ? 'no_new_matching_posts' : 'no_search_response'; break; }
          await page.mouse.wheel(0, 950);
          await page.waitForTimeout(2500);
        }
      } catch { status = 'page_error'; }
      finally {
        await Promise.all([...pending]);
        if (blocked) status = 'access_or_rate_limited';
        else if (errors) status = 'response_error';
        save();
        await page.close();
      }
      console.log(`${posts.size} snapshot mentah | ${status}`);
      if (status !== 'no_new_matching_posts') partial = true;
      if (['access_or_rate_limited', 'login_or_verification', 'response_error', 'no_search_response', 'page_error'].includes(status)) break;
    }
  } finally { await browser.close(); }
  return partial ? 1 : 0;
}

if (require.main === module) {
  main(process.argv[2]).then(code => { process.exitCode = code; }).catch(() => {
    console.error('Collector berhenti; periksa instalasi browser/session lokal. Data checkpoint tetap lokal.');
    process.exitCode = 1;
  });
}
module.exports = {candidates, normalize};
