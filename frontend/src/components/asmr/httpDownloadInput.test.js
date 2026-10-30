import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const panelApiMocks = vi.hoisted(() => ({
  preview: vi.fn(),
  start: vi.fn(),
  status: vi.fn(),
  health: vi.fn(),
  httpHealth: vi.fn(),
}))

vi.mock('../../api', () => ({
  baiduNetdiskApi: {
    preview: panelApiMocks.preview,
    start: panelApiMocks.start,
    status: panelApiMocks.status,
    health: panelApiMocks.health,
  },
  httpDownloadApi: {
    health: panelApiMocks.httpHealth,
  },
}))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}))

import HttpDownloadPanel from './HttpDownloadPanel.vue'
import {
  baiduInputLineMatchesStartedItem,
  isPikPakPassCodeLine,
  normalizeHttpDownloadInputRows,
  normalizePikPakShareText,
  pikPakShareIdentity,
} from './httpDownloadInput.js'

describe('PikPak 下载输入', () => {
  it('把下一行提取码合并到对应分享链接', () => {
    expect(normalizeHttpDownloadInputRows([
      'https://mypikpak.com/s/share-a',
      '提取码：A1b2',
      'https://example.com/file.zip',
    ])).toEqual([
      'https://mypikpak.com/s/share-a?pwd=A1b2',
      'https://example.com/file.zip',
    ])
  })

  it('从完整分享文案中提取链接和密码', () => {
    expect(normalizePikPakShareText(
      '分享给你：https://drive.mypikpak.com/s/share-b 访问码: 9z8y',
    )).toBe('https://drive.mypikpak.com/s/share-b?pwd=9z8y')
  })

  it('身份比较忽略提取码并能识别独立密码行', () => {
    expect(pikPakShareIdentity('https://mypikpak.com/s/share-c?pwd=abcd'))
      .toBe(pikPakShareIdentity('https://mypikpak.com/s/share-c'))
    expect(isPikPakPassCodeLine('密码：abcd')).toBe(true)
    expect(isPikPakPassCodeLine('https://mypikpak.com/s/share-c')).toBe(false)
  })
})

it('不重复输出PikPak链接并支持片段提取码', () => {
  expect(normalizeHttpDownloadInputRows([
    '分享给你：https://drive.mypikpak.com/s/share-b 访问码: 9z8y',
  ])).toEqual([
    'https://drive.mypikpak.com/s/share-b?pwd=9z8y',
  ])
  expect(normalizeHttpDownloadInputRows([
    'https://drive.mypikpak.com/s/share-b#提取码:9z8y',
  ])).toEqual([
    'https://drive.mypikpak.com/s/share-b?pwd=9z8y',
  ])
})

describe('百度标准化输入行清理', () => {
  it('插件结构化行按分享身份匹配已启动项目并忽略提取码', () => {
    const line = 'RJ01736954 | https://pan.baidu.com/s/1started | 提取码: ab12 | 解压码: secret'
    const startedItem = {
      share_url: 'https://pan.baidu.com/s/1started?pwd=ab12',
    }
    const otherItem = {
      share_url: 'https://pan.baidu.com/s/1other?pwd=cd34',
    }

    expect(baiduInputLineMatchesStartedItem(line, startedItem)).toBe(true)
    expect(baiduInputLineMatchesStartedItem(line, otherItem)).toBe(false)
    expect(baiduInputLineMatchesStartedItem(
      'RJ00000001 | https://pan.baidu.com/s/1other | 提取码: cd34 | 解压码: other-secret',
      otherItem,
    )).toBe(true)
  })
})

describe('百度任务创建后的输入框清理', () => {
  const startedLine = 'RJ01736954 | https://pan.baidu.com/s/1started | 提取码: ab12 | 解压码: secret'
  const pendingLine = 'RJ00000001 | https://pan.baidu.com/s/1pending | 提取码: cd34 | 解压码: keep'
  const otherPlatformLine = 'https://example.com/keep.zip'
  const startedUrl = 'https://pan.baidu.com/s/1started?pwd=ab12'

  beforeEach(() => {
    vi.clearAllMocks()
    panelApiMocks.health.mockResolvedValue({ ok: true })
    panelApiMocks.httpHealth.mockResolvedValue({ ok: true })
    panelApiMocks.preview.mockResolvedValue({
      items: [{
        ok: true,
        source: 'baidu_netdisk',
        share_url: startedUrl,
        filename: 'started.zip',
        size: 128,
      }],
      failed_count: 0,
      extracted_archive_passwords: [],
    })
  })

  async function mountPanelAndStart() {
    const wrapper = mount(HttpDownloadPanel, {
      props: { provider: 'baidu' },
      global: {
        stubs: {
          'el-dialog': { template: '<div><slot /></div>' },
        },
      },
    })
    await flushPromises()
    await wrapper.find('textarea.http-url-input').setValue([
      startedLine,
      pendingLine,
      otherPlatformLine,
    ].join('\n'))
    await wrapper.find('.http-actions button').trigger('click')
    await flushPromises()

    const startButton = wrapper.find('.http-actions button.is-primary')
    expect(startButton.attributes('disabled')).toBeUndefined()
    await startButton.trigger('click')
    await flushPromises()
    return wrapper
  }

  function expectOnlyStartedLineRemoved(wrapper) {
    expect(wrapper.find('textarea.http-url-input').element.value).toBe([
      pendingLine,
      otherPlatformLine,
    ].join('\n'))
  }

  it('正常创建成功后只删除本次已启动的结构化行', async () => {
    panelApiMocks.start.mockResolvedValue({
      tasks: [{ task_id: 'normal-task' }],
      message: '任务已创建',
    })

    const wrapper = await mountPanelAndStart()

    expect(panelApiMocks.start).toHaveBeenCalledOnce()
    expect(wrapper.emitted('started')?.[0]?.[0]).toEqual(['normal-task'])
    expectOnlyStartedLineRemoved(wrapper)
    wrapper.unmount()
  })

  it('请求超时但接回已创建任务后同样只删除已启动行', async () => {
    panelApiMocks.start.mockRejectedValue({
      code: 'ECONNABORTED',
      message: 'timeout of 60000ms exceeded',
    })
    panelApiMocks.status.mockResolvedValue({
      tasks: [{
        id: 'recovered-task',
        created_at: new Date().toISOString(),
        task_metadata: { download_mode: 'baidu_netdisk' },
      }],
    })

    const wrapper = await mountPanelAndStart()

    expect(panelApiMocks.status).toHaveBeenCalledOnce()
    expect(wrapper.emitted('started')?.[0]?.[0]).toEqual(['recovered-task'])
    expectOnlyStartedLineRemoved(wrapper)
    wrapper.unmount()
  })
})
