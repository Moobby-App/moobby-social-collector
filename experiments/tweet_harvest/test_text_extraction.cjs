const assert = require('node:assert/strict');
const {selectText} = require('./text_extraction.cjs');
const fs = require('node:fs');
const sample = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const observed = sample.response_text_candidates[0];
for (const wrap of [x => x, x => ({tweet:x})]) {
  const result = selectText(wrap({legacy:{full_text:observed.legacy_full_text}, note_tweet:{note_tweet_results:{result:{text:observed.note_tweet_text}}}}));
  assert.equal(result.full_text, sample.expanded_page_text);
  assert.equal(result.text_extraction_method, 'note_tweet');
  assert.equal(result.legacy_full_text, observed.legacy_full_text);
}
for (const text of [undefined, '', '  ', null]) {
  const result = selectText({legacy:{full_text:'Fallback',truncated:true},note_tweet:{note_tweet_results:{result:{text}}}});
  assert.equal(result.full_text, 'Fallback');
  assert.equal(result.text_completeness,'truncated');
}
assert.equal(selectText({legacy:{full_text:'Main'},quoted_status_result:{result:{note_tweet:{note_tweet_results:{result:{text:'Quote'}}}}}}).full_text,'Main');
console.log('Lulus: teks nyata cocok dengan halaman, nested tweet, fallback, dan quoted post terpisah.');
