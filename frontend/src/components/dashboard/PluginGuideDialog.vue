<template>
  <el-dialog
    v-model="visible"
    width="780px"
    append-to-body
    class="plugin-guide-dialog"
    @open="loadScripts"
  >
    <template #header>
      <div class="guide-header">
        <div class="guide-header-main">
          <span class="guide-title">插件使用说明</span>
          <span class="guide-tag">Tampermonkey</span>
        </div>
        <p class="guide-sub">两个脚本都只是「网页 → 本机后端」的搬运工，装好后配置一次即可长期使用。</p>
      </div>
    </template>

    <div class="guide-body">
      <div class="guide-prereq">
        <strong>前置条件</strong>
        <span>浏览器安装 Tampermonkey（或 Violentmonkey）；KikoeruManager 后端保持运行。</span>
      </div>

      <section
        v-for="script in scripts"
        :key="script.key"
        class="guide-card"
      >
        <header class="guide-card-head">
          <div class="guide-card-title">
            <h3>{{ script.name }}</h3>
            <span v-if="script.version" class="guide-version">v{{ script.version }}</span>
          </div>
          <div class="guide-card-actions">
            <el-button
              v-if="script.available"
              type="primary"
              size="small"
              plain
              @click="downloadScript(script)"
            >
              下载脚本
            </el-button>
            <span v-else class="guide-missing">当前安装未内置，请从仓库 tampermonkey/ 获取</span>
          </div>
        </header>

        <p class="guide-card-summary">{{ script.summary }}</p>

        <ol class="guide-steps">
          <li v-for="(step, index) in stepsFor(script.key)" :key="index">{{ step }}</li>
        </ol>

        <div v-if="script.key === 'panai'" class="guide-note">
          <span class="guide-note-label">可发送到下载面板的来源</span>
          <span class="guide-chips">
            <em v-for="item in script.supports" :key="item">{{ item }}</em>
          </span>
        </div>
      </section>

      <section class="guide-card guide-faq">
        <h3>常见问题</h3>
        <dl>
          <dt>点了「发送到下载」没反应</dt>
          <dd>先确认收件箱地址只写到端口（如 <code>http://10.0.0.5:5555</code>），不要带 <code>/api</code>；再确认令牌与「设置 → HTTP 下载 → 下载收件箱」里的一致。</dd>

          <dt>提示跨域被拦截 / 连接失败</dt>
          <dd>远程部署时后端地址要能被浏览器访问；若经过反向代理，请同时放行 <code>/api/download-inbox/*</code>。插件已声明 <code>@connect *</code>，仍失败时多为网络或端口不通。</dd>

          <dt>Transfer.it 链接能识别，但下载失败</dt>
          <dd>transfer.it 是端到端加密传输，必须由后端解密拉取，浏览器直链下载拿不到文件；后端需能访问外网，失败时任务详情会给出具体原因。</dd>

          <dt>VoiceLinks 装了但 RJ 号没变链接</dt>
          <dd>在 Tampermonkey 菜单里把当前域名加入白名单（脚本不默认处理所有站点），并确认设置里 KikoeruManager 地址填对。</dd>

          <dt>脚本更新</dt>
          <dd>回到本页重新下载覆盖安装即可；两个脚本都不会改你的库存，也不会把数据发到第三方。</dd>
        </dl>
      </section>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiUrl, userscriptApi } from '../../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const scripts = ref([])
const loading = ref(false)

const STEPS = {
  panai: [
    '安装 Tampermonkey 扩展（已装可跳过），确认浏览器允许运行用户脚本。',
    '点「下载脚本」，在 Tampermonkey 安装页确认安装。',
    '打开「设置 → HTTP 下载 → 下载收件箱」：勾选「启用下载收件箱」。跨机器或经反代访问时，关闭「局域网免令牌」并生成访问令牌。',
    '点页面浮动图标 →「识别助手配置」：收件箱地址填本机后端地址（默认 http://localhost:5555），访问令牌与上一步一致（未设令牌则留空）。',
    '在任意网页选中含网盘链接的文字，点「发送到下载」；任务会进入 HTTP 下载面板，勾选文件后开始下载。'
  ],
  voicelinks: [
    '安装 Tampermonkey 扩展，点「下载脚本」并确认安装。',
    '首次使用：Tampermonkey 菜单 →「VoiceLinks：添加当前域名到白名单」，否则 RJ 号不会自动变成链接。',
    '打开脚本设置面板 →「搜索」→ 把 KikoeruManager 地址改成本机后端地址（默认 http://localhost:5555）。',
    '在 DLsite、South Plus、Telegram Web 等页面看到 RJ 号即可直接点开；链接后的圆点表示是否已入库（绿=有、红=无、白=查询失败）。'
  ]
}

function stepsFor(key) {
  return STEPS[key] || []
}

async function loadScripts() {
  if (loading.value) return
  loading.value = true
  try {
    const data = await userscriptApi.list()
    scripts.value = Array.isArray(data?.scripts) ? data.scripts : []
  } catch (error) {
    scripts.value = []
    ElMessage.error('读取内置脚本信息失败，可从仓库 tampermonkey/ 目录手动获取')
  } finally {
    loading.value = false
  }
}

function downloadScript(script) {
  if (!script?.key) return
  window.open(apiUrl(`/userscripts/${script.key}/download`), '_blank', 'noopener')
}
</script>

<style scoped>
.guide-header-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.guide-title {
  font-size: 17px;
  font-weight: 650;
  color: var(--el-text-color-primary);
}
.guide-tag {
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
}
.guide-sub {
  margin: 6px 0 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.guide-body {
  max-height: 66vh;
  overflow-y: auto;
  padding-right: 4px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.guide-prereq {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 9px 12px;
  border-radius: 8px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
}
.guide-prereq strong {
  color: var(--el-text-color-primary);
  white-space: nowrap;
}
.guide-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  padding: 14px 16px;
  background: var(--el-bg-color);
}
.guide-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.guide-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.guide-card-title h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  color: var(--el-text-color-primary);
}
.guide-version {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}
.guide-card-actions {
  display: flex;
  align-items: center;
}
.guide-missing {
  font-size: 11.5px;
  color: var(--el-color-warning);
}
.guide-card-summary {
  margin: 6px 0 10px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.guide-steps {
  margin: 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
}
.guide-note {
  margin-top: 10px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.guide-note-label {
  color: var(--el-text-color-regular);
}
.guide-chips {
  display: inline-flex;
  gap: 6px;
  flex-wrap: wrap;
}
.guide-chips em {
  font-style: normal;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11.5px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
}
.guide-faq h3 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 650;
  color: var(--el-text-color-primary);
}
.guide-faq dl {
  margin: 0;
}
.guide-faq dt {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  margin-top: 8px;
}
.guide-faq dt:first-child {
  margin-top: 0;
}
.guide-faq dd {
  margin: 3px 0 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
.guide-faq code {
  padding: 0 4px;
  border-radius: 4px;
  font-size: 11.5px;
  background: var(--el-fill-color-light);
}
</style>
