import os
import json
import urllib.request as request

BROWSERLESS_API_KEY = "2TvZjOV4kMKp3ZD7009d45f7e0e806f3107a6c45065776c59"
BROWSERLESS_REGION = "sfo"

JS_CODE = """
export default async ({ page }) => {
  const searchUrl = 'https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm';
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 800 });

  try {
    await page.goto(searchUrl, { waitUntil: 'networkidle2', timeout: 60000 });
    
    // Extract info about select dropdowns
    const selectsInfo = await page.evaluate(() => {
      const selects = Array.from(document.querySelectorAll('select'));
      return selects.map(sel => {
        const options = Array.from(sel.querySelectorAll('option')).map(opt => ({
          value: opt.value,
          text: opt.innerText.trim()
        }));
        
        let labelText = '';
        if (sel.id) {
            const label = document.querySelector(`label[for="${sel.id}"]`);
            if (label) labelText = label.innerText.trim();
        }
        
        // Try to get preceding text nodes or td contents if no label
        if (!labelText) {
            let parentTd = sel.closest('td');
            if (parentTd && parentTd.previousElementSibling) {
                labelText = parentTd.previousElementSibling.innerText.trim();
            }
        }

        return {
          name: sel.name,
          id: sel.id,
          label: labelText,
          options: options
        };
      });
    });

    return { selects: selectsInfo };
  } catch (e) {
    return { error: e.message };
  }
}
"""

url = f"https://production-{BROWSERLESS_REGION}.browserless.io/function?token={BROWSERLESS_API_KEY}"
payload = {"code": JS_CODE}
data = json.dumps(payload).encode('utf-8')
headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}

try:
    req = request.Request(url, data=data, headers=headers)
    with request.urlopen(req, timeout=120) as response:
        result = json.loads(response.read().decode())
        print(json.dumps(result, indent=2))
except Exception as e:
    print(f"Error: {e}")
