// Browser comparison only. No credentials or complete responses are exported.
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('./node_modules/playwright');
const {selectText} = require('./text_extraction.cjs');

function candidates(value, output = []) {
  if (Array.isArray(value)) {
    for (const child of value) candidates(child, output);
  } else if (value && typeof value === 'object') {
    if (value.tweet_results?.result) output.push(value.tweet_results.result);
    for (const [key, child] of Object.entries(value)) {
      if (!['tweet_results','quoted_status_result','retweeted_status_result'].includes(key)) candidates(child, output);
    }
  }
  return output;
}

function normalize(result, account, since, until) {
  const tweet = result.legacy ? result : result.tweet;
  if (!tweet?.legacy) return null;
  const legacy = tweet.legacy;
  const user = tweet.core?.user_results?.result;
  const username = user?.core?.screen_name || user?.legacy?.screen_name;
  if (!username || username.toLowerCase() !== account.toLowerCase()) return null;
  if (legacy.in_reply_to_status_id_str || legacy.in_reply_to_user_id_str || legacy.retweeted_status_result || tweet.retweeted_status_result || /^RT\s+@/.test(legacy.full_text || '')) return null;
  const published = Date.parse(legacy.created_at);
  if (!Number.isFinite(published) || published < since || published > until) return null;
  const extracted = selectText(result);
  return {
    id: String(tweet.rest_id || legacy.id_str), username,
    source_url: `https://x.com/${username}/status/${tweet.rest_id || legacy.id_str}`,
    published_at: new Date(published).toISOString(),
    full_text: extracted.full_text,
    text_extraction_method: extracted.text_extraction_method,
    media: JSON.parse(extracted.media_json),
    image_count: extracted.image_count,
    media_extraction_basis: extracted.media_extraction_basis,
  };
}

async function main() {
  const account = process.argv[2] || 'commuterline';
  const limit = Number(process.argv[3] || '20');
  if (!/^[A-Za-z0-9_]{1,15}$/.test(account) || !Number.isInteger(limit) || limit < 1 || limit > 100) throw Error('Invalid account/limit');
  const root = path.resolve(__dirname, '../..');
  const session = path.join(root, 'playwright/.auth/x_reused_session.json');
  if (!fs.existsSync(session)) throw Error('Saved session missing');
  const until = Date.now(), since = until - 24*60*60*1000;
  const stamp = new Date(until).toISOString().replace(/[-:.]/g,'');
  const folder = path.join(root,'data/source_comparisons',`${account}_${stamp}`);
  fs.mkdirSync(folder,{recursive:true});
  const browser = await chromium.launch({headless:false});
  const collected = {};
  try {
    for (const method of ['search','profile']) {
      const context = await browser.newContext({storageState:session});
      const page = await context.newPage();
      const records = new Map(), pending = new Set();
      let blocked = false, reason = 'scroll_limit', errors = 0;
      let relevantResponses = 0;
      page.on('response', response => {
        const url = new URL(response.url());
        if (!(url.hostname === 'x.com' || url.hostname.endsWith('.x.com'))) return;
        if (response.status() === 429) blocked = true;
        const wanted = method === 'search' ? url.pathname.includes('SearchTimeline') : /\/(UserTweets|UserTweetsAndReplies|UserOriginalsTimeline)$/.test(url.pathname);
        if (!wanted || !response.ok()) return;
        const task = (async () => {
          try {
            const body = await response.json();
            relevantResponses++;
            for (const result of candidates(body)) {
              const row = normalize(result,account,since,until);
              if (row) records.set(row.id,row);
            }
          } catch { errors++; }
        })();
        pending.add(task); task.finally(() => pending.delete(task));
      });
      const query = `from:${account} -filter:replies -filter:retweets since:${new Date(since).toISOString().slice(0,10)}`;
      const url = method === 'search' ? `https://x.com/search?q=${encodeURIComponent(query)}&src=typed_query&f=live` : `https://x.com/${account}`;
      const startedAt = new Date().toISOString();
      try {
        await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000});
        await page.waitForTimeout(5000);
        let stale = 0, previous = 0;
        for (let round=0; round<40; round++) {
          await Promise.all([...pending]);
          if (blocked) {reason='rate_limited';break;}
          if (/\/login|\/onboarding\/|\/account\/access/.test(page.url())) {reason='login_or_verification';break;}
          if (records.size>=limit) {reason='target_reached';break;}
          stale = records.size === previous ? stale + 1 : 0;
          previous = records.size;
          const nearBottom = await page.evaluate(() => window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 1000);
          if (stale>=5 && nearBottom) {reason='no_new_matching_posts_at_page_end';break;}
          await page.mouse.wheel(0,850);
          await page.waitForTimeout(2000);
        }
        await Promise.all([...pending]);
      } catch { reason='page_error'; }
      const allRows = [...records.values()].sort((a,b)=>b.published_at.localeCompare(a.published_at));
      const result = {method,started_at:startedAt,finished_at:new Date().toISOString(),stop_reason:reason,relevant_responses:relevantResponses,parse_errors:errors,observed_matching_count:allRows.length,posts:allRows.slice(0,limit)};
      collected[method] = result;
      fs.writeFileSync(path.join(folder,method+'.json'),JSON.stringify(result,null,2));
      console.log(`${method}: ${result.posts.length} posts; stop=${reason}; responses=${relevantResponses}`);
      await context.close();
      if (blocked || reason==='login_or_verification') break;
    }
  } finally {await browser.close();}
  const search = new Map((collected.search?.posts || []).map(row=>[row.id,row]));
  const profile = new Map((collected.profile?.posts || []).map(row=>[row.id,row]));
  const shared = [...search.keys()].filter(id=>profile.has(id));
  const report = {account,target:limit,window_since:new Date(since).toISOString(),window_until:new Date(until).toISOString(),
    search_count:search.size,profile_count:profile.size,shared_ids:shared,
    only_search:[...search.keys()].filter(id=>!profile.has(id)),only_profile:[...profile.keys()].filter(id=>!search.has(id)),
    shared_text_differences:shared.filter(id=>search.get(id).full_text!==profile.get(id).full_text),
    shared_media_differences:shared.filter(id=>JSON.stringify(search.get(id).media)!==JSON.stringify(profile.get(id).media)),
    limitation:'Bounded sample in a fixed 24h window, not a completeness or latency benchmark. Quote posts included. Pinned posts are subject to the same time filter.'};
  fs.writeFileSync(path.join(folder,'comparison.json'),JSON.stringify(report,null,2));
  console.log(`Shared ${shared.length}; only search ${report.only_search.length}; only profile ${report.only_profile.length}.`);
  console.log('Output:',folder);
}

if (require.main === module) main().catch(() => {console.error('Comparison failed; inspect local setup/session. No credentials exported.');process.exitCode=1;});
module.exports = {candidates,normalize};
