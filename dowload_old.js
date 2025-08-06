const fs = require('fs');
const path = require('path');
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');
const puppeteer = require('puppeteer');
const ffmpegPath = require('ffmpeg-static');
const { spawn } = require('child_process');
require('dotenv').config();

// Support legacy AWS env vars like AWS_ACCESS_KEY by mapping them to
// AWS_ACCESS_KEY_ID if the standard variable is not set.
if (!process.env.AWS_ACCESS_KEY_ID && process.env.AWS_ACCESS_KEY) {
  process.env.AWS_ACCESS_KEY_ID = process.env.AWS_ACCESS_KEY;
}



// ─── S3 CLIENT SETUP ───────────────────────────────────────────────────────────
const REGION = process.env.AWS_REGION || "eu-north-1";
const s3 = new S3Client({ region: REGION });

/**
 * Upload an object to S3 with exponential-backoff retry on SlowDown.
 * On non-retryable errors, marks the error so caller can retry with a buffer.
 */
async function uploadWithRetry(params, maxRetries = 5) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await s3.send(new PutObjectCommand(params));
    } catch (err) {
      // Retry on S3 SlowDown
      if (err.Code === 'SlowDown' && attempt < maxRetries) {
        const backoff = Math.pow(2, attempt) * 100;
        const jitter  = Math.random() * 100;
        await new Promise(res => setTimeout(res, backoff + jitter));
        continue;
      }
      // Signal to caller this error can be retried using a buffered upload
      err._canBuffer = true;
      throw err;
    }
  }
}




async function ensureDir(dir) {
  await fs.promises.mkdir(dir, { recursive: true });
}

async function evaluateWithRetry(page, fn, retries = 2) {
  for (let i = 0; i <= retries; i++) {
    try {
      return await page.evaluate(fn);
    } catch (err) {
      if (err.message.includes('Execution context was destroyed') && i < retries) {
        await page.waitForNavigation({ waitUntil: 'networkidle2' }).catch(() => {});
        continue;
      }
      throw err;
    }
  }
}

async function uploadFileToS3(filePath, key, bucket, metadata = {}) {
  const fileStream = fs.createReadStream(filePath);
  const params = {
    Bucket: bucket,
    Key: key,
    Body: fileStream,
    Metadata: metadata,
  };
  return uploadWithRetry(params);
}

async function uploadBufferToS3(buffer, key, bucket, metadata = {}, contentType = 'video/mp4') {
  const params = { Bucket: bucket, Key: key, Body: buffer, Metadata: metadata, ContentType: contentType };
  return uploadWithRetry(params);
}

async function convertM3u8ToMp4(url, outputPath, headers = {}) {
  const headerArgs = [];
  if (Object.keys(headers).length) {
    const lines = Object.entries(headers)
      .map(([k, v]) => `${k}: ${v}\r\n`)
      .join('');
    headerArgs.push('-headers', lines);
  }

  return new Promise((resolve, reject) => {
    const ffArgs = [
      '-hide_banner',          // suppress build/config info
      '-loglevel', 'error',    // only show errors
      '-y',                    // overwrite output
      ...headerArgs,
      '-i', url,
      '-c', 'copy',
      '-bsf:a', 'aac_adtstoasc',
      outputPath,
    ];

    const ff = spawn(ffmpegPath, ffArgs);

    // if you still want to see ffmpeg errors:
    ff.stderr.on('data', data => process.stderr.write(data));
    ff.on('close', code => {
      if (code === 0) resolve();
      else reject(new Error(`ffmpeg exited with code ${code}`));
    });
  });
}


async function downloadFile(url, dest, headers = {}) {
  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(`Failed to download ${url}: ${res.status}`);
  }
  // Read the entire response into memory
  const arrayBuffer = await res.arrayBuffer();
  // Write it out as a Buffer
  await fs.promises.writeFile(dest, Buffer.from(arrayBuffer));
}

async function downloadAndUpload(url, key, bucket, headers = {}, metadata = {}) {
  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(`Failed to download ${url}: ${res.status}`);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  await uploadBufferToS3(buf, key, bucket, metadata, res.headers.get('content-type') || 'video/mp4');
}


async function downloadPanopto(moodleInput, { headless = false, s3Key, skipLocal = false } = {}) {
  const arcProfile = process.env.ARC_PROFILE_DIR;
  if (!arcProfile) throw new Error('ARC_PROFILE_DIR is not set');
  const downloadDir = process.env.DOWNLOAD_DIR || path.join(__dirname, '..', 'downloads');
  const s3Bucket = process.env.S3_BUCKET;
  if (!skipLocal) {
    await ensureDir(downloadDir);
  }

  // ─── Launch or connect ─────────────────────────────────────────────────────
  let browser;
  const connectEndpoint = process.env.CHROME_WS_ENDPOINT;
  if (connectEndpoint) {
    browser = await puppeteer.connect({ browserWSEndpoint: connectEndpoint });
  } else {
    browser = await puppeteer.launch({
      headless,
      userDataDir: arcProfile,
      args: [
        '--window-size=1920,1080',
        `--user-data-dir=${arcProfile}`,
        '--enable-features=NetworkService,NetworkServiceInProcess',
      ],
      defaultViewport: null,
    });
  }

  // ─── Go to Moodle and find Panopto link ────────────────────────────────────
  const page = await browser.newPage();
  const moodleUrl = /^https?:/.test(moodleInput)
    ? moodleInput
    : `https://moodle2024.u-pariscite.fr/mod/url/view.php?id=${moodleInput}`;
  await page.goto(moodleUrl, { waitUntil: 'networkidle2' });

  const structure = await evaluateWithRetry(page, () => {
    const selectors = ['.page-navbar', '.page-nav', 'nav', '.breadcrumb'];
    let nav;
    for (const sel of selectors) {
      nav = document.querySelector(sel);
      if (nav) break;
    }
    if (!nav) return [];
    return Array.from(nav.querySelectorAll('li a, a'))
      .map(a => a.textContent.trim())
      .filter(Boolean);
  });

  let panoptoLink = await evaluateWithRetry(page, () => {
    const anchors = Array.from(document.querySelectorAll('a[href*="Panopto"]'));
    return anchors.find(a => /Viewer\.aspx/.test(a.href))?.href || null;
  });

  const shouldCloseBrowser = !process.env.CHROME_WS_ENDPOINT;
  if (!panoptoLink) {
    if (/Panopto\/Pages\/Viewer\.aspx/i.test(moodleUrl)) {
      panoptoLink = moodleUrl;
    } else {
      console.error('Panopto link not found');
      if (shouldCloseBrowser) await browser.close();
      return;
    }
  }

  // ─── Open Panopto viewer and prepare to catch DeliveryInfo ────────────────
  const panoptoPage = await browser.newPage();

  // Hook the JSON response *before* navigation
  const deliveryPromise = panoptoPage.waitForResponse(
    resp =>
      resp
        .url()
        .includes('DeliveryInfo.aspx') &&
      resp.status() === 200,
    { timeout: 20_000 }
  );

  await panoptoPage.goto(panoptoLink, { waitUntil: 'networkidle2' });

  // Grab auth cookies
  const cookies = await panoptoPage.cookies();
  const cookieHeader = cookies.map(c => `${c.name}=${c.value}`).join('; ');

  // Await the DeliveryInfo JSON
  let info;
  try {
    const deliveryResp = await deliveryPromise;
    info = await deliveryResp.json();
    const sessionName = info?.Delivery?.SessionName;
  } catch (err) {
    console.error('Timed out waiting for DeliveryInfo.aspx:', err.message);
    if (shouldCloseBrowser) await browser.close();
    return;
  }

  // Extract the combined MP4 URL
  const mp4Url = info?.Delivery?.PodcastStreams?.[0]?.StreamUrl;
  if (!mp4Url) {
    console.error('Could not find PodcastStreams[0].StreamUrl in DeliveryInfo');
    if (shouldCloseBrowser) await browser.close();
    return;
  }

  // ─── Download the combined A+V MP4 ────────────────────────────────────────
  const streamName = info?.Delivery?.SessionName;
  const key = s3Key || path.join(structure.join('/'), 'lecture.mp4');

  console.log('Downloading MP4 from:', mp4Url);
  try {
    if (s3Bucket) {
      if (skipLocal) {
        console.log(`Uploading directly to s3://${s3Bucket}/${key}`);
        await downloadAndUpload(mp4Url, key, s3Bucket, { Cookie: cookieHeader }, { name: streamName });
        console.log(`Uploaded to s3://${s3Bucket}/${key}`);
      } else {
        const targetPath = path.join(downloadDir, structure.join('/'));
        await ensureDir(targetPath);
        const outFile = path.join(targetPath, 'lecture.mp4');
        await downloadFile(mp4Url, outFile, { Cookie: cookieHeader });
        console.log(`Uploading ${outFile} to s3://${s3Bucket}/${key}`);
        try {
          await uploadFileToS3(outFile, key, s3Bucket, { name: streamName });
          console.log(`Uploaded to s3://${s3Bucket}/${key}`);
        } catch (err) {
          console.error(`Failed to upload to s3://${s3Bucket}/${key}:`, err);
        }
        console.log(`Saved to ${outFile}`);
      }
    } else {
      if (skipLocal) {
        console.log('S3_BUCKET not set, skipping download.');
      } else {
        const targetPath = path.join(downloadDir, structure.join('/'));
        await ensureDir(targetPath);
        const outFile = path.join(targetPath, 'lecture.mp4');
        await downloadFile(mp4Url, outFile, { Cookie: cookieHeader });
        console.log(`Saved to ${outFile}`);
      }
    }
  } catch (err) {
    console.error('Failed to download MP4:', err);
  }

  // ─── Cleanup ───────────────────────────────────────────────────────────────
    // instead of browser.close(), just close the two pages/tabs we opened:
    await panoptoPage.close().catch(() => {});
    await page.close().catch(() => {});
  
}




async function main() {
  const args = process.argv.slice(2);
  if (args[0] === '--test') {
    const url = args[1];
    if (!url) {
      console.error('Usage: node src/download.js --test <url>');
      process.exit(1);
    }
    await downloadPanopto(url, { headless: true });
    process.exit(0);

  }

  if (args[0] === '--run') {
    const filePath = args[1] || 'urls.txt';
    if (!fs.existsSync(filePath)) {
      console.error(`URL list not found: ${filePath}`);
      process.exit(1);
    }
    const urls = fs.readFileSync(filePath, 'utf-8')
      .split(/\r?\n/)
      .filter(Boolean);
    for (const url of urls) {
      try {
        await downloadPanopto(url);
      } catch (err) {
        console.error('Failed to download', url, err);
      }
    }
    return;
  }

  const modeArg = args.find(a => a.startsWith('--mode=') || a.startsWith('--mode-'));
  if (modeArg) {
    const mode = modeArg.startsWith('--mode=')
      ? modeArg.slice('--mode='.length)
      : modeArg.slice('--mode-'.length);

    if (!process.env.S3_BUCKET) {
      process.env.S3_BUCKET = `actu-${mode.toLowerCase()}`;
    }

    if (mode.toUpperCase() === 'UPC') {
      const data = JSON.parse(fs.readFileSync('upc.json', 'utf8'));
      for (const [field, entries] of Object.entries(data)) {
        let index = 1;
        for (const url of Object.values(entries)) {
          const key = path.join(field, `${field} ${index}.mp4`);
          try {
            console.log(`Downloading ${url} to s3://${process.env.S3_BUCKET}/${key}`);
            await downloadPanopto(url, { headless: true, s3Key: key, skipLocal: true });
          } catch (err) {
            console.error('Failed to download', url, err);
          }
          index++;
        }
      }
      return;
    } else if (mode.toUpperCase() === 'SORBONNE') {
      const listFile = 'sorbonne.txt';
      if (!fs.existsSync(listFile)) {
        console.error(`URL list not found: ${listFile}`);
        process.exit(1);
      }
      const urls = fs.readFileSync(listFile, 'utf8')
        .split(/\r?\n/)
        .filter(Boolean);
      let index = 1;
      for (const url of urls) {
        const key = path.join('Sorbonne', `Sorbonne ${index}.mp4`);
        try {
          console.log(`Downloading ${url} to s3://${process.env.S3_BUCKET}/${key}`);
          await downloadPanopto(url, { headless: true, s3Key: key, skipLocal: true });
        } catch (err) {
          console.error('Failed to download', url, err);
        }
        index++;
      }
      return;
    } else {
      console.error(`Unknown mode: ${mode}`);
      process.exit(1);
    }
  }

  const input = args[0];
  if (!input) {
    console.error('Usage: node src/download.js <moodleIdOrUrl>');
    console.error('   or: node src/download.js --test <url>');
    console.error('   or: node src/download.js --run [list.txt]');
    console.error('   or: node src/download.js --mode-UPC');
    console.error('   or: node src/download.js --mode-Sorbonne');
    process.exit(1);
  }

  await downloadPanopto(input);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});