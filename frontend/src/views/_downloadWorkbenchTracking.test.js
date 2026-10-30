import { flushPromises, mount, shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const downloadViewMocks = vi.hoisted(() => ({
  route: { path: '/asmr-sync', query: { tab: 'http' } },
  routerReplace: vi.fn(),
  asmrStatus: vi.fn(),
  waitingRetry: vi.fn(),
  configGet: vi.fn(),
  httpStatus: vi.fn(),
  httpRetry: vi.fn(),
  baiduStatus: vi.fn(),
  inboxPending: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => downloadViewMocks.route,
  useRouter: () => ({
    replace: downloadViewMocks.routerReplace,
  }),
}))
vi.mock('../composables/useViewport', () => ({
  useViewport: () => ({ isMobile: { value: false } }),
}))
vi.mock('../api', () => ({
  asmrSyncApi: {
    status: downloadViewMocks.asmrStatus,
    getWaitingRetry: downloadViewMocks.waitingRetry,
  },
  configApi: { get: downloadViewMocks.configGet },
  httpDownloadApi: {
    status: downloadViewMocks.httpStatus,
    retry: downloadViewMocks.httpRetry,
  },
  baiduNetdiskApi: { status: downloadViewMocks.baiduStatus },
  downloadInboxApi: { pending: downloadViewMocks.inboxPending },
  libraryApi: {},
  taskApi: {},
  taskCenterApi: {},
}))

vi.mock('@lottiefiles/dotlottie-vue', () => ({
  DotLottieVue: { template: '<div />' },
}))
vi.mock('@/components/common/AppLottieProgressBar.vue', () => ({
  default: { template: '<div />' },
}))
vi.mock('@/components/common/AppEmptyState.vue', () => ({
  default: { template: '<div />' },
}))

import DownloadTaskWorkbenchDialog from '../components/download/DownloadTaskWorkbenchDialog.vue'
import ASMRSync from './ASMRSync.vue'
import {
  createLatestRequestGuard,
  mergeTrackedDownloadTaskIds,
  replaceTrackedDownloadTaskId,
  selectTrackedDownloadTasks,
} from './_downloadWorkbenchTracking.js'

describe('下载工作台任务跟踪', () => {
  it('追加任务时把新任务置顶并去重', () => {
    expect(mergeTrackedDownloadTaskIds(['old-1', 'old-2'], ['new-1', 'old-1']))
      .toEqual(['new-1', 'old-1', 'old-2'])
  })

  it('legacy 等待重试创建新任务后替换旧跟踪 ID', () => {
    expect(replaceTrackedDownloadTaskId(
      ['legacy-waiting', 'other-task'],
      'legacy-waiting',
      'retry-task',
    )).toEqual(['retry-task', 'other-task'])
  })

  it('旧状态快照缺少新任务时不修改跟踪 ID', () => {
    const trackedIds = ['new-1', 'old-1']
    const visibleTasks = selectTrackedDownloadTasks(trackedIds, [
      { id: 'old-1', status: 'processing' },
    ])

    expect(visibleTasks.map(task => task.id)).toEqual(['old-1'])
    expect(trackedIds).toEqual(['new-1', 'old-1'])
  })

  it('状态返回顺序变化时仍按工作台跟踪顺序展示', () => {
    const visibleTasks = selectTrackedDownloadTasks(
      ['task-2', 'task-1'],
      [{ id: 'task-1' }, { id: 'task-2' }],
    )

    expect(visibleTasks.map(task => task.id)).toEqual(['task-2', 'task-1'])
  })

  it('只接受最后发起的状态请求', () => {
    const guard = createLatestRequestGuard()
    const firstRequest = guard.begin()
    const secondRequest = guard.begin()

    expect(guard.isLatest(firstRequest)).toBe(false)
    expect(guard.isLatest(secondRequest)).toBe(true)
  })

  it('关闭工作台后拒绝仍在途的状态响应', () => {
    const guard = createLatestRequestGuard()
    const pendingRequest = guard.begin()

    guard.invalidate()

    expect(guard.isLatest(pendingRequest)).toBe(false)
  })

  it('社团补全追加批次后拒绝旧批次状态覆盖', () => {
    const guard = createLatestRequestGuard()
    const oldBatchRequest = guard.begin()
    const trackedIds = mergeTrackedDownloadTaskIds(['old-task'], ['new-task'])
    const newBatchRequest = guard.begin()

    expect(guard.isLatest(oldBatchRequest)).toBe(false)
    expect(guard.isLatest(newBatchRequest)).toBe(true)
    expect(trackedIds).toEqual(['new-task', 'old-task'])
    expect(selectTrackedDownloadTasks(trackedIds, [{ id: 'old-task' }]))
      .toEqual([{ id: 'old-task' }])
    expect(trackedIds).toEqual(['new-task', 'old-task'])
  })
})

describe('失败下载任务重试入口', () => {
  it('Gofile 零文件整体失败也显示重试并回传原任务', async () => {
    const task = {
      id: 'gofile-empty-failure',
      work_title: 'Gofile 下载',
      status: 'failed',
      display_status: 'failed',
      error_message: '没有通过校验的下载项',
      download_mode: 'gofile',
      download_files: [],
      failed_files: [],
      task_metadata: {
        download_mode: 'gofile',
        session_id: '',
      },
    }
    const wrapper = mount(DownloadTaskWorkbenchDialog, {
      props: {
        visible: true,
        embedded: true,
        mergeTasks: false,
        tasks: [task],
      },
      global: {
        stubs: {
          DotLottieVue: true,
          AppLottieProgressBar: true,
          AppEmptyState: true,
        },
      },
    })

    const retryButton = wrapper.find('button.retry')
    expect(retryButton.exists()).toBe(true)
    await retryButton.trigger('click')
    expect(wrapper.emitted('retry-task')?.[0]?.[0]).toMatchObject({
      id: 'gofile-empty-failure',
    })
  })

  it('工作台 retry-task 事件由父级调用 HTTP 整任务重试 API', async () => {
    downloadViewMocks.asmrStatus.mockResolvedValue({ tasks: [] })
    downloadViewMocks.waitingRetry.mockResolvedValue({ tasks: [] })
    downloadViewMocks.configGet.mockResolvedValue({ storage: {}, asmr_sync: {} })
    downloadViewMocks.httpStatus.mockResolvedValue({ tasks: [] })
    downloadViewMocks.httpRetry.mockResolvedValue({ ok: true })
    downloadViewMocks.baiduStatus.mockResolvedValue({ tasks: [] })
    downloadViewMocks.inboxPending.mockResolvedValue({ items: [] })
    downloadViewMocks.routerReplace.mockResolvedValue()

    const wrapper = shallowMount(ASMRSync, {
      global: {
        stubs: Object.fromEntries([
          'el-table',
          'el-table-column',
          'el-switch',
          'el-input',
          'el-option',
          'el-select',
          'el-pagination',
          'el-button',
          'el-dialog',
          'el-drawer',
        ].map(name => [name, true])),
        directives: { 'app-loading': {} },
      },
    })
    await flushPromises()

    const workbench = wrapper.findComponent(DownloadTaskWorkbenchDialog)
    expect(workbench.exists()).toBe(true)
    workbench.vm.$emit('retry-task', { id: 'gofile-empty-failure' })
    await flushPromises()

    expect(downloadViewMocks.httpRetry).toHaveBeenCalledWith('gofile-empty-failure')
    expect(downloadViewMocks.httpStatus).toHaveBeenCalled()
    wrapper.unmount()
  })
})
