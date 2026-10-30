const PIKPAK_HOSTS = new Set(['mypikpak.com', 'www.mypikpak.com', 'drive.mypikpak.com'])
const PASS_CODE_QUERY_KEYS = ['pwd', 'pass_code', 'passcode', 'password', 'code']
const PASS_CODE_FULL = /^[A-Za-z0-9]{2,32}$/
const URL_RE = /https?:\/\/[^\s<>"'\u3000-\u303f\u4e00-\u9fff]+/gi
const NO_SCHEME_URL_RE = /(?<![A-Za-z0-9])(?:(?:pan|yun|eyun)\.baidu\.com|(?:mypikpak|drive\.mypikpak)\.com|(?:www\.)?gofile\.io|(?:www\.)?transfer\.it|1drv\.ms|onedrive\.live\.com|onedrive\.com|drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)(?:\/[^\s<>"'\u3000-\u303f\u4e00-\u9fff]*)?/gi
const MARKDOWN_URL_RE = /\[[^\]]*\]\((https?:\/\/[^)\s]+)\)/gi
const HTML_HREF_RE = /href\s*=\s*["'](https?:\/\/[^"']+)["']/gi
const ARCHIVE_KEYWORDS = ['解压', '解壓', '压缩', '壓縮', 'rar', 'zip', '7z', 'archive', 'unzip', 'extract']

function normalizeLinkText(text) {
  return String(text || '')
    .replace(/[\u200b\u200c\u200d\ufeff\u2060]/g, '')
    .replace(/[\uFF01-\uFF5E]/g, ch => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0))
    .replace(/点|點/g, '.')
    .replace(/％/g, '%')
}

function validPassCode(value) {
  const code = String(value || '').trim()
  return PASS_CODE_FULL.test(code) ? code : ''
}

function cleanUrlCandidate(raw) {
  let url = String(raw || '').trim()
  if (!url) return null
  let code = ''
  const separator = url.match(/(?:----|---|--)\s*([A-Za-z0-9]{2,32})\s*$/i)
  if (separator) {
    code = validPassCode(separator[1])
    url = url.slice(0, separator.index)
  }
  const trailingLabel = url.match(/(?<![?&=])(?:提取码|提取口令|访问码|密[码碼]|pwd|passcode|pass_code|password|code|key)\s*[:：=]?\s*([A-Za-z0-9]{2,32})$/i)
  if (trailingLabel) {
    code = validPassCode(trailingLabel[1])
    url = url.slice(0, trailingLabel.index)
  } else {
    const bareTrailing = url.match(/:([A-Za-z0-9]{2,32})$/)
    if (bareTrailing) {
      code = validPassCode(bareTrailing[1])
      url = url.slice(0, bareTrailing.index)
    }
  }
  while (/[。，；：、！？)\]}>》」】'"`]$/.test(url)) url = url.slice(0, -1)
  while (url.endsWith('#')) url = url.slice(0, -1)
  if (!/^https?:\/\//i.test(url)) url = `https://${url}`
  try {
    new URL(url)
  } catch {
    return null
  }
  return { url, code }
}

function extractUrlCandidates(text) {
  const candidates = []
  const seen = new Set()
  const add = (raw, start, end) => {
    const cleaned = cleanUrlCandidate(raw)
    if (!cleaned) return
    const key = cleaned.url.replace(/\/+$/g, '')
    if (seen.has(key)) return
    seen.add(key)
    candidates.push({ ...cleaned, start, end })
  }
  for (const match of text.matchAll(MARKDOWN_URL_RE)) add(match[1], match.index, match.index + match[0].length)
  for (const match of text.matchAll(HTML_HREF_RE)) add(match[1], match.index, match.index + match[0].length)
  for (const match of text.matchAll(URL_RE)) add(match[0], match.index, match.index + match[0].length)
  for (const match of text.matchAll(NO_SCHEME_URL_RE)) add(match[0], match.index, match.index + match[0].length)
  candidates.sort((a, b) => a.start - b.start)
  return candidates
}

function platformForUrl(url) {
  try {
    const host = new URL(url).hostname.toLowerCase()
    if (host.includes('baidu.com')) return 'baidu'
    if (host.includes('mypikpak.com')) return 'pikpak'
    if (host.includes('gofile.io')) return 'gofile'
    if (host.includes('transfer.it')) return 'transferit'
    if (host.includes('1drv.ms') || host.includes('onedrive.')) return 'onedrive'
    if (host.includes('google.com') && (host.includes('drive.') || host.includes('docs.'))) return 'google_drive'
  } catch {}
  return ''
}

function passCodeFromUrl(url) {
  try {
    const parsed = new URL(String(url || ''))
    for (const key of PASS_CODE_QUERY_KEYS) {
      const value = parsed.searchParams.get(key)
      const code = validPassCode(value)
      if (code) return code
    }
    const fragment = decodeURIComponent(parsed.hash.replace(/^#/, ''))
    if (!fragment) return ''
    const body = /^(?:pwd|password|passcode|pass_code|code|p)=(.+)$/i.test(fragment)
      ? fragment.replace(/^(?:pwd|password|passcode|pass_code|code|p)=/i, '')
      : fragment
    return validPassCode(body)
  } catch {
    return ''
  }
}

function segmentHasArchiveContext(value) {
  const text = String(value || '').toLowerCase()
  return ARCHIVE_KEYWORDS.some(keyword => text.includes(keyword))
}

function extractPassCodeFromSegment(value, allowBare = false) {
  const segment = String(value || '').trim()
  if (!segment) return ''
  const archiveContext = segmentHasArchiveContext(segment)
  const labelPattern = archiveContext
    ? /(?:提取码|提取口令|访问码|pwd|passcode|pass_code|code|key)\s*[:：=]?\s*([A-Za-z0-9]{2,32})/i
    : /(?:提取码|提取口令|访问码|pwd|passcode|pass_code|password|code|key)\s*[:：=]?\s*([A-Za-z0-9]{2,32})/i
  const explicit = segment.match(labelPattern)
  if (explicit) return validPassCode(explicit[1])
  if (!archiveContext) {
    const generic = segment.match(/密[码碼]\s*[:：=]?\s*([A-Za-z0-9]{2,32})/)
    if (generic) return validPassCode(generic[1])
  }
  if (allowBare) {
    const bare = segment.match(/[A-Za-z0-9]{2,32}/)
    if (bare) return validPassCode(bare[0])
  }
  return ''
}

function shareHasCode(value) {
  return Boolean(String(value || '').match(/[?&](?:pwd|passcode|pass_code|password|code)=/i))
}

function appendSharePassCode(shareUrl, code) {
  const normalizedUrl = String(shareUrl || '').trim().replace(/#$/, '')
  const normalizedCode = validPassCode(code)
  if (!normalizedUrl || !normalizedCode || shareHasCode(normalizedUrl)) return normalizedUrl
  try {
    const parsed = new URL(normalizedUrl)
    parsed.searchParams.set('pwd', normalizedCode)
    return parsed.toString()
  } catch {
    return normalizedUrl
  }
}

function shareIdentity(value) {
  try {
    const parsed = new URL(String(value || '').trim())
    for (const key of PASS_CODE_QUERY_KEYS) parsed.searchParams.delete(key)
    parsed.hash = ''
    return parsed.toString().replace(/\/+$/g, '')
  } catch {
    return String(value || '').trim()
  }
}

function extractShareUrls(text, platformFilter = '') {
  const normalized = normalizeLinkText(text)
  const result = []
  const seen = new Map()
  let lastIndex = null
  for (const line of normalized.split(/\r?\n/)) {
    const candidates = extractUrlCandidates(line)
    if (candidates.length) {
      let previousEnd = 0
      candidates.forEach((candidate, index) => {
        const platform = platformForUrl(candidate.url)
        if (platformFilter && platform !== platformFilter) {
          previousEnd = candidate.end
          return
        }
        const after = line.slice(candidate.end, index + 1 < candidates.length ? candidates[index + 1].start : line.length)
        const before = line.slice(previousEnd, candidate.start)
        previousEnd = candidate.end
        let code = validPassCode(candidate.code)
        if (!code) code = passCodeFromUrl(candidate.url)
        if (!code) code = extractPassCodeFromSegment(after, true)
        if (!code) code = extractPassCodeFromSegment(before, false)
        const shareUrl = appendSharePassCode(candidate.url, code)
        const identity = shareIdentity(shareUrl)
        if (seen.has(identity)) {
          const existingIndex = seen.get(identity)
          if (!shareHasCode(result[existingIndex]) && shareHasCode(shareUrl)) result[existingIndex] = shareUrl
          lastIndex = existingIndex
        } else {
          result.push(shareUrl)
          seen.set(identity, result.length - 1)
          lastIndex = result.length - 1
        }
      })
      continue
    }
    const code = extractPassCodeFromSegment(line, true)
    if (code && lastIndex !== null && !shareHasCode(result[lastIndex])) {
      result[lastIndex] = appendSharePassCode(result[lastIndex], code)
    }
  }
  return [...new Set(result)]
}

export function extractBaiduShareUrls(rows) {
  return extractShareUrls((rows || []).join('\n'), 'baidu')
}

const BAIDU_STRUCTURED_SEPARATOR_RE = /\s*[|｜]\s*|\t+|\s+-{3,}\s+/
const BAIDU_PASS_CODE_LABEL_RE = /^(?:提取码|提取口令|访问码|pwd|passcode|pass_code)\s*[:：=]/i

// 插件规范格式（RJ | 链接 | 提取码: x | 解压码: y）只把链接和提取码
// 交给分享解析；解压码不能被裸串兜底误当成百度提取码。
export function normalizeBaiduStructuredLine(value) {
  const text = String(value || '').trim()
  if (!text || !/[|｜\t]/.test(text)) return text
  const segments = text.split(BAIDU_STRUCTURED_SEPARATOR_RE).map(item => item.trim()).filter(Boolean)
  if (segments.length < 2) return text
  const urlSegment = segments.find(item => platformForUrl(item) === 'baidu')
  if (!urlSegment) return text
  const passSegment = segments.find(item => BAIDU_PASS_CODE_LABEL_RE.test(item))
  const code = passSegment ? extractPassCodeFromSegment(passSegment) : ''
  return code ? appendSharePassCode(urlSegment, code) : urlSegment
}

export function baiduInputLineMatchesStartedItem(line, item) {
  const normalizedLine = normalizeBaiduStructuredLine(line)
  const [lineUrl] = extractBaiduShareUrls([normalizedLine])
  const itemUrl = String(item?.share_url || item?.url || item?.masked_url || '').trim()
  if (!lineUrl || platformForUrl(itemUrl) !== 'baidu') return false
  const lineIdentity = shareIdentity(lineUrl)
  const itemIdentity = shareIdentity(itemUrl)
  return Boolean(lineIdentity && itemIdentity && lineIdentity === itemIdentity)
}

export function extractHttpShareUrls(rows) {
  return extractShareUrls((rows || []).join('\n'), '')
}

export function pikPakPassCodeFromText(value) {
  return extractPassCodeFromSegment(value, true)
}

export function normalizePikPakShareText(value) {
  const match = normalizeLinkText(value).match(/https?:\/\/(?:www\.|drive\.)?mypikpak\.com\/[^\s<>"'）)]+/i)
  if (!match) return ''
  return appendSharePassCode(match[0].replace(/[.,，。;；]+$/g, ''), pikPakPassCodeFromText(value))
}

export function pikPakShareIdentity(value) {
  const shareUrl = normalizePikPakShareText(value)
  if (!shareUrl) return ''
  try {
    const parsed = new URL(shareUrl)
    if (!PIKPAK_HOSTS.has(parsed.hostname.toLowerCase())) return ''
    return `${parsed.hostname.toLowerCase()}${parsed.pathname.replace(/\/+$/g, '')}`
  } catch {
    return ''
  }
}

export function isPikPakPassCodeLine(value) {
  const text = String(value || '').trim()
  return Boolean(text && !normalizePikPakShareText(text) && pikPakPassCodeFromText(text))
}

export function normalizeHttpDownloadInputRows(rows) {
  return extractHttpShareUrls(rows)
}