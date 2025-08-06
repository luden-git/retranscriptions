#!/usr/bin/env node
// download_from_tree.js

import dotenv from 'dotenv';
dotenv.config();  // load .env

import fs from 'fs/promises';
import { createReadStream } from 'fs';
import path from 'path';
import puppeteer from 'puppeteer-core';
import fetch from 'node-fetch';
import { pipeline } from 'stream/promises';
import {
  S3Client,
  HeadBucketCommand,
  PutObjectCommand
} from '@aws-sdk/client-s3';
import { Upload } from '@aws-sdk/lib-storage';

import { fileURLToPath } from 'url';
const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

// ─── Config ──────────────────────────────────────────────────────────
const TREE_PATH   = process.argv[2];
const WS_ENDPOINT = process.env.CHROME_WS_ENDPOINT;
const BUCKET      = process.env.S3_BUCKET;
const REGION      = process.env.region || 'eu-north-1';

if (!TREE_PATH || !WS_ENDPOINT || !BUCKET) {
  console.error('Usage: node download_from_tree.js <tree.json>');
  console.error('Make sure .env has CHROME_WS_ENDPOINT and S3_BUCKET set.');
  process.exit(1);
}

// ─── S3 Client ───────────────────────────────────────────────────────
const s3 = new S3Client({
  region: REGION,
  endpoint: `https://s3.${REGION}.amazonaws.com`,
  forcePathStyle: true,
  credentials: {
    accessKeyId:     process.env.AWS_ACCESS_KEY_ID,
    secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
  }
});
await s3.send(new PutObjectCommand({
  Bucket: BUCKET,
  Key:    'test.txt',
  Body:   'hello world'
}));
console.log('Test file uploaded');


// sanitize folder names for S3
function sanitize(name) {
  return name.replace(/[\/\\#?%&{}<>*:|"^~\[\]`]+/g, '').trim();
}

// ─── download & upload helper ───────────────────────────────────────
async function downloadSingle(browser, courseName, downloadDir, { url, title }, idx, total) {
  // build filename and forcedownload URL
  const parsed = new URL(url);
  const ext    = path.extname(parsed.pathname).split('?')[0] || '';
  let filename;
  if (/^Resource\s+\d+/i.test(title)) {
    filename = decodeURIComponent(parsed.pathname.split('/').pop());
  } else {
    filename = title.replace(/[\/\\#?%&{}<>*:|"^~\[\]`]+/g, '').trim() + ext;
  }
  if (!parsed.searchParams.has('forcedownload') &&
      ['.mp4','.pdf','.pptx','.ppsm'].includes(ext.toLowerCase())) {
    parsed.searchParams.set('forcedownload','1');
  }
  const downloadUrl = parsed.toString();
  const filePath    = path.join(downloadDir, filename);

  console.log(`☁️  [${idx}/${total}] Starting download → ${filename}`);
  // grab cookies from your logged-in browser
  const page = await browser.newPage();
  let cookies = [];
  try {
    // this may ERR_ABORTED on direct download URLs—catch it and move on
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 10000 });
  } catch (err) {
    console.warn(`   ⚠️  Navigation aborted (expected for attachments): ${err.message}`);
  }
  cookies = await page.cookies();
  await page.close();
  const cookieHeader = cookies.map(c => `${c.name}=${c.value}`).join('; ');

  // 3) Fetch the entire file in one go
  console.log(`   🚀 Fetching: ${downloadUrl}`);
  let res;
  try {
    res = await fetch(downloadUrl, { headers: { Cookie: cookieHeader } });
  } catch (err) {
    console.warn(`   ⚠️  Fetch error: ${err.message}`);
    return;
  }
  console.log(`   ⬇️  Response: HTTP ${res.status}  content-type=${res.headers.get('content-type')}`);
  if (!res.ok) {
    console.warn(`   ⚠️  Aborting: HTTP ${res.status}`);
    return;
  }

  // 4) Buffer & write to disk
  console.log(`   💾  Writing to: ${filePath}`);
  let buffer;
  try {
    const arrayBuf = await res.arrayBuffer();
    buffer = Buffer.from(arrayBuf);
    await fs.writeFile(filePath, buffer);
    console.log(`   ✅  Saved to disk: ${filePath}`);
  } catch (err) {
    console.warn(`   ⚠️  Write error: ${err.message}`);
    return;
  }
  

  // 5) Upload to S3 using that same buffer
  const key = `${sanitize(courseName)}/${filename}`;

  
console.log(`   📤 Starting multipart upload to s3://${BUCKET}/${key}`);

// Use the lib-storage Upload helper to get progress events
const parallelUpload = new Upload({
  client: s3,
  params: {
    Bucket: BUCKET,
    Key:    key,
    Body:   buffer
  },
});

// Listen for progress events
parallelUpload.on('httpUploadProgress', (progress) => {
  // progress.loaded / progress.total
  const done = progress.loaded.toLocaleString();
  const total = progress.total ? progress.total.toLocaleString() : '???';
  console.log(`   📈 ${done} / ${total} bytes uploaded`);
});

try {
  // `.done()` returns once the full upload is finished
  await parallelUpload.done();
  console.log('   ✅ Upload complete');
} catch (err) {
  console.error('   ❌ Upload failed:', err.message);
}

  // 6) Cleanup
  await fs.unlink(filePath).catch(() => {});
  console.log('   🗑️  Temp file removed');

}

// ─── Main ────────────────────────────────────────────────────────────
;(async () => {
  // load JSON tree
  let tree;
  try {
    tree = JSON.parse(await fs.readFile(TREE_PATH, 'utf8'));
  } catch (err) {
    console.error('❌ Error loading tree:', err.message);
    process.exit(1);
  }

  // connect to existing Chrome/Arc
  const browser = await puppeteer.connect({ browserWSEndpoint: WS_ENDPOINT });

  // for each course
  for (const [courseName, node] of Object.entries(tree)) {
    // flatten resources
    const items = [];
    (function walk(n) {
      (n.resources.pdfs   || []).forEach(u=>items.push({url:u,title:n.title}));
      (n.resources.mp4    || []).forEach(u=>items.push({url:u,title:n.title}));
      (n.resources.others || []).forEach(u=>items.push({url:u,title:n.title}));
      (n.children         || []).forEach(c=>walk(c));
    })(node);

    if (items.length === 0) {
      console.log(`ℹ️  "${courseName}" has no files, skipping`);
      continue;
    }

    console.log(`\n📂 Course: ${courseName} (${items.length} files)`);
    const downloadDir = path.join(__dirname, 'tmp', sanitize(courseName));
    await fs.rm(downloadDir, { force:true, recursive:true });
    await fs.mkdir(downloadDir, { recursive:true });

    for (let i = 0; i < items.length; i++) {
      await downloadSingle(browser, courseName, downloadDir, items[i], i+1, items.length);
    }
  }

  console.log('\n🎉 All done!');
  await browser.disconnect();
})().catch(err => {
  console.error('❌ Fatal error:', err);
  process.exit(1);
});
