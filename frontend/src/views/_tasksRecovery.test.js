import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import Tasks from './Tasks.vue'
import TaskDetailPane from '../components/tasks/TaskDetailPane.vue'

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  getItem: vi.fn(),
  restoreFilteredItem: vi.fn(),
  confirm: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
  push: vi.fn(),
  virtualizerMode: 'empty',
}))

vi.mock('../api', () => ({
  taskCenterApi: {
    list: mocks.list,
    getItem: mocks.getItem,
    restoreFilteredItem: mocks.restoreFilteredItem,
    action: vi.fn(),
    getItemFiles: vi.fn(),
    deleteLibraryItem: vi.fn(),
  },
  libraryApi: {
    browserDelete: vi.fn(),
  },
}))

vi.mock('../composables/useSystemPrompt', () => ({
  showSystemConfirm: mocks.confirm,
}))

vi.mock('../composables/useViewport', async () => {
  const { ref } = await import('vue')
  return { useViewport: () => ({ isMobile: ref(false) }) }
})

vi.mock('../composables/useRealtimeEvents', async () => {
  const { ref } = await import('vue')
  return { useRealtimeEvents: () => ({ connected: ref(true) }) }
})

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
}))

vi.mock('element-plus', () => ({
  ElMessage: {
    success: mocks.success,
    warning: mocks.warning,
    error: mocks.error,
  },
}))

vi.mock('@tanstack/vue-virtual', async () => {
  const { computed } = await import('vue')
  return {
    useVirtualizer: (options) => computed(() => ({
      count: options.value.count,
      getVirtualItems: () => {
        if (mocks.virtualizerMode === 'stale' && options.value.count > 1) {
          return [{ index: 0, key: 'stale-row', start: 100000, size: 34 }]
        }
        return []
      },
      getTotalSize: () => options.value.count * 34,
      measure: vi.fn(),
    })),
  }
})

const task = {
  id: 'task-1',
  title: '测试任务',
  status: 'completed',
  status_label: '已完成',
  actions: [],
  details: { metadata: { filter_recovery: { target_ready: true } } },
}

async function mountTasks() {
  const wrapper = mount(Tasks, {
    global: {
      stubs: {
        TasksHeader: true,
        TasksFilters: true,
        TaskListPane: true,
        TaskDetailPane: { name: 'TaskDetailPane', props: ['fileTreeSections'], template: '<div />' },
      },
    },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

async function mountTasksWithDetail() {
  const wrapper = mount(Tasks, {
    global: {
      stubs: {
        TasksHeader: true,
        TasksFilters: true,
        TaskListPane: true,
      },
    },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('任务中心批量恢复', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    mocks.list.mockResolvedValue({ items: [task], total: 1 })
    mocks.getItem.mockResolvedValue(task)
    mocks.confirm.mockResolvedValue(true)
  })

  it('逐项恢复并汇总部分失败', async () => {
    mocks.restoreFilteredItem
      .mockResolvedValueOnce({ message: '已还原到库存' })
      .mockRejectedValueOnce({ response: { data: { detail: '目标已存在' } } })
    const wrapper = await mountTasks()
    const detail = wrapper.findComponent({ name: 'TaskDetailPane' })

    detail.vm.$emit('restore-filtered', {
      entries: [
        { key: 'a.txt', label: 'a.txt', type: 'file', recoveryId: 'recovery-1', recoveryKey: 'recovery-1' },
        { key: 'b.txt', label: 'b.txt', type: 'file', recoveryId: 'recovery-2', recoveryKey: 'recovery-2' },
      ],
    })
    await flushPromises()
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalledTimes(1)
    expect(mocks.restoreFilteredItem).toHaveBeenNthCalledWith(1, 'task-1', 'recovery-1', '')
    expect(mocks.restoreFilteredItem).toHaveBeenNthCalledWith(2, 'task-1', 'recovery-2', '')
    expect(mocks.warning).toHaveBeenCalledWith(expect.stringContaining('已还原 1 项，1 项失败'))
    expect(mocks.warning).toHaveBeenCalledWith(expect.stringContaining('目标已存在'))
    expect(mocks.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('批量全部成功时汇总成功数量', async () => {
    mocks.restoreFilteredItem.mockResolvedValue({ message: '已还原到库存' })
    const wrapper = await mountTasks()
    const detail = wrapper.findComponent({ name: 'TaskDetailPane' })

    detail.vm.$emit('restore-filtered', {
      entries: [
        { key: 'a.txt', label: 'a.txt', type: 'file', recoveryId: 'recovery-1', recoveryKey: 'recovery-1' },
        { key: 'b.txt', label: 'b.txt', type: 'file', recoveryId: 'recovery-2', recoveryKey: 'recovery-2' },
      ],
    })
    await flushPromises()
    await flushPromises()

    expect(mocks.restoreFilteredItem).toHaveBeenCalledTimes(2)
    expect(mocks.success).toHaveBeenCalledWith('已还原 2 项到库存')
    expect(mocks.warning).not.toHaveBeenCalled()
    expect(mocks.error).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('批量全部失败时继续逐项处理并汇总原因', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    mocks.restoreFilteredItem
      .mockRejectedValueOnce(new Error('第一项失败'))
      .mockRejectedValueOnce(new Error('第二项失败'))
    const wrapper = await mountTasks()
    const detail = wrapper.findComponent({ name: 'TaskDetailPane' })

    detail.vm.$emit('restore-filtered', {
      entries: [
        { key: 'a.txt', label: 'a.txt', type: 'file', recoveryId: 'recovery-1', recoveryKey: 'recovery-1' },
        { key: 'b.txt', label: 'b.txt', type: 'file', recoveryId: 'recovery-2', recoveryKey: 'recovery-2' },
      ],
    })
    await flushPromises()
    await flushPromises()

    expect(mocks.restoreFilteredItem).toHaveBeenCalledTimes(2)
    expect(mocks.error).toHaveBeenCalledWith(expect.stringContaining('a.txt（第一项失败）'))
    expect(mocks.error).toHaveBeenCalledWith(expect.stringContaining('b.txt（第二项失败）'))
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.warning).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('保留单项恢复调用和成功反馈', async () => {
    mocks.restoreFilteredItem.mockResolvedValue({ message: '单文件已还原' })
    const wrapper = await mountTasks()
    const detail = wrapper.findComponent({ name: 'TaskDetailPane' })

    detail.vm.$emit('restore-filtered', {
      entry: { key: 'a.txt', label: 'a.txt', type: 'file', recoveryId: 'recovery-1', recoveryKey: 'recovery-1' },
    })
    await flushPromises()
    await flushPromises()

    expect(mocks.restoreFilteredItem).toHaveBeenCalledWith('task-1', 'recovery-1', '')
    expect(mocks.success).toHaveBeenCalledWith('单文件已还原')
    wrapper.unmount()
  })

  it('最终库存树保留已删除目录的历史子项并可逐文件恢复', async () => {
    const treeTask = {
      ...task,
      details: {
        metadata: {
          filter_recovery: { target_ready: true },
          final_file_tree_root_label: 'RJ00000001',
          final_file_tree_items: [
            { relative_path: '已删除目录', name: '已删除目录', type: 'dir' },
            { relative_path: '现存文件.wav', name: '现存文件.wav', type: 'file' },
          ],
          file_tree_items: [
            { relative_path: '已删除目录', name: '已删除目录', type: 'dir' },
            { relative_path: '已删除目录/子目录', name: '子目录', type: 'dir' },
            { relative_path: '已删除目录/子目录/台本.txt', name: '台本.txt', type: 'file', size: 12 },
            { relative_path: '仅存于解压阶段的临时文件.tmp', name: '临时文件.tmp', type: 'file' },
          ],
          filtered_items: [{
            relative_path: '已删除目录',
            name: '已删除目录',
            type: 'dir',
            recovery_id: 'recovery-dir',
            recovery_status: 'available',
          }],
        },
      },
    }
    mocks.list.mockResolvedValue({ items: [treeTask], total: 1 })
    mocks.getItem.mockResolvedValue(treeTask)
    const wrapper = await mountTasks()
    await flushPromises()
    await flushPromises()

    const sections = wrapper.findComponent({ name: 'TaskDetailPane' }).props('fileTreeSections')
    expect(sections).toHaveLength(1)
    const rows = sections[0].rows
    const restoredFile = rows.find(row => row.key === 'RJ00000001/已删除目录/子目录/台本.txt')

    expect(rows.some(row => row.key === 'RJ00000001/已删除目录/子目录')).toBe(true)
    expect(restoredFile).toMatchObject({
      status: 'removed',
      recoveryId: 'recovery-dir',
      recoveryRelativePath: '子目录/台本.txt',
    })
    expect(rows.some(row => String(row.key).includes('临时文件.tmp'))).toBe(false)
    wrapper.unmount()
  })
})

describe('任务文件树虚拟列表', () => {
  it('派生文件树折叠后重新展开仍渲染文件行', async () => {
    const treeTask = {
      ...task,
      details: {
        metadata: {
          file_tree_view_kind: 'extracted_snapshot',
          extracted_file_tree_root_label: 'RJ01435183',
          extracted_file_tree_items: [
            { relative_path: '01.正篇音声', name: '01.正篇音声', type: 'dir' },
            { relative_path: '01.正篇音声/01.wav', name: '01.wav', type: 'file', size: 12 },
            { relative_path: '02.附赠/插画', name: '插画', type: 'dir' },
            { relative_path: '02.附赠/插画/主插画.png', name: '主插画.png', type: 'file', size: 34 },
          ],
        },
      },
    }
    mocks.list.mockResolvedValue({ items: [treeTask], total: 1 })
    mocks.getItem.mockResolvedValue(treeTask)
    const wrapper = await mountTasksWithDetail()
    const findTreeToggle = () => wrapper.findAll('button').find(button => (
      ['展开文件树', '收起文件树'].includes(button.text())
    ))

    expect(findTreeToggle()?.text()).toBe('收起文件树')
    await findTreeToggle().trigger('click')
    await flushPromises()
    expect(findTreeToggle()?.text()).toBe('展开文件树')

    await findTreeToggle().trigger('click')
    await flushPromises()
    await flushPromises()

    expect(findTreeToggle()?.text()).toBe('收起文件树')
    expect(wrapper.findAll('.task-file-tree-row').length).toBeGreaterThan(0)
    expect(wrapper.text()).toContain('01.wav')
    expect(wrapper.text()).toContain('主插画.png')
    wrapper.unmount()
  })

  it('小文件树展开时不采用虚拟器失效坐标', async () => {
    mocks.virtualizerMode = 'stale'
    const treeTask = {
      ...task,
      details: {
        metadata: {
          file_tree_view_kind: 'extracted_snapshot',
          extracted_file_tree_items: [
            { relative_path: '目录', name: '目录', type: 'dir' },
            { relative_path: '目录/01.wav', name: '01.wav', type: 'file', size: 12 },
          ],
        },
      },
    }
    mocks.list.mockResolvedValue({ items: [treeTask], total: 1 })
    mocks.getItem.mockResolvedValue(treeTask)
    let wrapper
    try {
      wrapper = await mountTasksWithDetail()
      const findTreeToggle = () => wrapper.findAll('button').find(button => (
        ['展开文件树', '收起文件树'].includes(button.text())
      ))
      await findTreeToggle().trigger('click')
      await flushPromises()
      await findTreeToggle().trigger('click')
      await flushPromises()
      await flushPromises()

      const canvas = wrapper.find('.task-file-tree-virtual-canvas')
      const canvasHeight = Number.parseFloat(canvas.element.style.height)
      const rows = wrapper.findAll('.task-file-tree-row')
      expect(rows.length).toBeGreaterThan(0)
      for (const row of rows) {
        const translateY = Number.parseFloat(row.element.style.transform.match(/translateY\(([\d.]+)px\)/)?.[1] || '0')
        expect(translateY).toBeLessThan(canvasHeight)
      }
    } finally {
      wrapper?.unmount()
      mocks.virtualizerMode = 'empty'
    }
  })

  it('虚拟器尚未测量时滚动仍能到达第40项之后', async () => {
    const rows = Array.from({ length: 120 }, (_, index) => ({
      key: `file-${index}`,
      label: `文件 ${index}`,
      relative_path: `file-${index}`,
      type: 'file',
      status: 'default',
      depth: 0,
      hasChildren: false,
      expanded: false,
      defaultExpanded: true,
    }))
    const wrapper = mount(TaskDetailPane, {
      props: {
        item: task,
        fileTreeSections: [{ key: 'file-list', rows }],
        formatRJCode: value => value,
        formatDateTime: value => value,
        showProgress: () => false,
        getRecoveredNotice: () => '',
        getDLsiteFailureReason: () => '',
        getOutputPath: () => '',
      },
    })
    await flushPromises()
    const scroller = wrapper.find('.task-file-tree')
    scroller.element.scrollTop = 90 * 34
    await scroller.trigger('scroll')
    await flushPromises()

    expect(wrapper.text()).toContain('文件 90')
    wrapper.unmount()
  })
})
