const assert = require('node:assert/strict');
const {selectText} = require('./text_extraction.cjs');
const photo = id => ({id_str:id,type:'photo',media_url_https:`https://pbs.twimg.com/media/${id}.jpg`});
const legacy = {full_text:'Caption',entities:{media:[photo('1')]},extended_entities:{media:[photo('1'),photo('2'),photo('3'),photo('4')]}};
for (const result of [{legacy},{tweet:{legacy}}]) {
  const out = selectText(result);
  assert.equal(out.image_count,4);
  assert.equal(JSON.parse(out.image_urls_json).length,4);
  assert.deepEqual(JSON.parse(out.media_json).map(x=>x.media_index),[1,2,3,4]);
  assert.equal(out.media_extraction_basis,'extended_entities');
}
const fallback=selectText({legacy:{entities:{media:[photo('1')]}}});
assert.equal(fallback.image_count,1);
assert.equal(fallback.media_extraction_basis,'entities');
const mixed=selectText({legacy:{extended_entities:{media:[photo('1'),photo('1'),{id_str:'2',type:'video',media_url_https:'https://pbs.twimg.com/video.jpg'}]}}});
assert.equal(mixed.media_count,2);
assert.equal(mixed.image_count,1);
assert.equal(JSON.parse(mixed.media_json)[1].image_url,null);
assert.equal(JSON.parse(mixed.media_json)[1].thumbnail_url,'https://pbs.twimg.com/video.jpg');
assert.equal(selectText({legacy:{},quoted_status_result:{result:{legacy}}}).media_count,0);
assert.equal(selectText({legacy:{}}).media_extraction_basis,'not_available');
console.log('Lulus: 4 foto, nested result, fallback, duplikat, video, dan quoted post.');
