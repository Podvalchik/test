const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [src, out] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + path.resolve(src), { waitUntil: 'networkidle' });
  await page.pdf({ path: out, format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:8px;color:#5a6577;width:100%;text-align:center;font-family:Inter,sans-serif">Диво-остров: концепция аналитического проекта · стр. <span class="pageNumber"></span> из <span class="totalPages"></span></div>' });
  await browser.close();
})();
