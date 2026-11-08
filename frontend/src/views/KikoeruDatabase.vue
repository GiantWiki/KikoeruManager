<template>
  <div class="kikoeru-db-page max-w-[1400px] mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">
    <AppPageHeader
      :icon="Database"
      icon-color="var(--km-nav-subtitle-icon, #6366f1)"
      title="Kikoeru 数据库"
      subtitle="直接管理远程 Kikoeru 的 SQLite 数据库：浏览、编辑、备份恢复与一键套用文件命名。"
    >
      <button class="page-head-btn ghost btn-diagnose" type="button" :disabled="diagnosing" :title="featureEnabled ? '' : '功能未启用，请先在设置中开启并保存'" @click="runDiagnose">
        <Loader2 v-if="diagnosing" :size="13" :stroke-width="2.4" class="animate-spin" />
        <Stethoscope v-else :size="13" :stroke-width="2.4" class="page-head-btn-icon" />
        <span class="page-head-btn-label">{{ diagnosing ? '检测中…' : '检测数据库' }}</span>
      </button>

      <button class="page-head-btn ghost btn-backup" type="button" :disabled="backingUp" :title="featureEnabled ? '' : '功能未启用，请先在设置中开启并保存'" @click="doBackup">
        <Loader2 v-if="backingUp" :size="13" :stroke-width="2.4" class="animate-spin" />
        <Save v-else :size="13" :stroke-width="2.4" class="page-head-btn-icon" />
        <span class="page-head-btn-label">{{ backingUp ? '备份中…' : '立即备份' }}</span>
      </button>

      <button
        class="page-head-btn ghost is-blue btn-rename"
        type="button"
        :disabled="!featureEnabled"
        :title="featureEnabled ? '' : '功能未启用，请先在设置中开启并保存'"
        @click="openRenameWizard(false)"
      >
        <Wand2 :size="13" :stroke-width="2.6" class="page-head-btn-icon" />
        <span class="page-head-btn-label">一键套用文件命名</span>
      </button>

      <button
        class="page-head-btn ghost is-blue btn-rating-fix"
        type="button"
        :disabled="!featureEnabled"
        :title="featureEnabled ? '' : '功能未启用，请先在设置中开启并保存'"
        @click="openRatingFixWizard(false)"
      >
        <Star :size="13" :stroke-width="2.6" class="page-head-btn-icon" />
        <span class="page-head-btn-label">评分修复</span>
      </button>

      <button
        class="page-head-btn ghost is-blue btn-cleanup"
        type="button"
        :disabled="!featureEnabled"
        :title="featureEnabled ? '对上次处理位置之后入库的作品做 title 替换与评分异常修复' : '功能未启用，请先在设置中开启并保存'"
        @click="openCleanupDialog"
      >
        <Loader2 v-if="cleanupStatusData.running" :size="13" :stroke-width="2.4" class="animate-spin" />
        <FolderSync v-else :size="13" :stroke-width="2.6" class="page-head-btn-icon" />
        <span class="page-head-btn-label">{{ cleanupStatusData.running ? '整理中…' : '增量整理' }}</span>
      </button>

      <button class="page-head-btn ghost btn-refresh icon-only" type="button" title="刷新" @click="refreshAll">
        <RefreshCw :size="13" :stroke-width="2.6" class="page-head-btn-icon" />
      </button>
    </AppPageHeader>

    <!-- 连接状态卡 -->
    <section class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      <div class="px-6 py-4 flex flex-wrap items-center gap-x-8 gap-y-2">
        <div class="flex items-center gap-2">
          <span class="inline-block size-2 rounded-full" :class="featureEnabled ? 'bg-emerald-500' : 'bg-slate-300'" />
          <span class="text-sm text-slate-700">{{ featureEnabled ? '功能已启用' : '功能未启用（请在设置中开启）' }}</span>
        </div>
        <div class="text-sm text-slate-500">
          数据库：<code class="text-xs bg-slate-100 rounded px-1.5 py-0.5">{{ dbPath || '未配置' }}</code>
        </div>
        <div class="text-sm text-slate-500">
          扫描监听：
          <el-tag v-if="scanStatus.listening" type="success" size="small" effect="plain">监听中</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">未运行</el-tag>
          <span v-if="scanStatus.last_scan_finished_at" class="ml-2 text-xs text-slate-400">
            上次扫描完成 {{ scanStatus.last_scan_finished_at }}
          </span>
        </div>
      </div>
    </section>

    <!-- 数据表浏览 -->
    <section class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      <div class="px-6 pt-4">
        <el-tabs v-model="activeTable">
          <el-tab-pane v-for="t in tables" :key="t.name" :name="t.name">
            <template #label>
              <span class="inline-flex items-center gap-1.5">
                {{ t.name }}
                <el-tag v-if="t.mode === 'readonly'" size="small" type="warning" effect="plain">只读</el-tag>
                <el-tag v-if="t.known === false" size="small" type="info" effect="plain">未识别</el-tag>
              </span>
            </template>
          </el-tab-pane>
        </el-tabs>
      </div>

      <div class="px-6 pb-2 flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-2">
          <el-input
            v-model="searchText"
            placeholder="搜索文本字段…"
            clearable
            class="!w-72"
            @keyup.enter="loadRows"
            @clear="loadRows"
          />
          <el-button :icon="Search" @click="loadRows">搜索</el-button>
        </div>
        <div class="flex items-center gap-2">
          <el-button
            v-if="currentMode === 'editable' && selectedRows.length > 0"
            type="primary"
            plain
            :icon="Wand2"
            @click="openRenameWizard(true)"
          >
            套用命名（已选 {{ selectedRows.length }} 行）
          </el-button>
          <el-button v-if="currentMode === 'editable'" :icon="Plus" @click="openCreateDialog">新增行</el-button>
          <el-button
            v-if="currentMode === 'editable'"
            type="danger"
            plain
            :disabled="selectedRows.length === 0"
            :icon="Trash2"
            @click="deleteSelected"
          >
            删除选中
          </el-button>
        </div>
      </div>

      <el-table
        ref="rowsTableRef"
        :data="rows"
        v-loading="loadingRows"
        size="small"
        border
        stripe
        class="w-full"
        @selection-change="onSelectionChange"
        @sort-change="onSortChange"
      >
        <el-table-column v-if="currentMode === 'editable'" type="selection" width="42" />
        <el-table-column
          v-for="col in displayColumns"
          :key="col.name"
          :prop="col.name"
          :label="col.name"
          :min-width="columnWidth(col)"
          show-overflow-tooltip
          sortable="custom"
          :sort-orders="['ascending', 'descending']"
        >
          <template #default="{ row }">
            <span class="text-xs">{{ renderCell(row[col.name]) }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="currentMode === 'editable'" label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEditDialog(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="px-6 py-4 flex justify-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100, 200]"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="loadRows"
          @size-change="loadRows"
        />
      </div>
    </section>

    <!-- 备份管理卡 -->
    <section class="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
      <div class="px-6 py-4 border-b border-slate-100 flex items-center justify-between bg-slate-50/50">
        <h2 class="text-base font-semibold text-slate-900">备份与恢复</h2>
        <div class="flex items-center gap-2">
          <el-button size="small" :icon="RefreshCw" @click="loadBackups">刷新</el-button>
          <el-button size="small" type="danger" plain :icon="Undo2" @click="restoreDialogVisible = true">一键恢复</el-button>
        </div>
      </div>
      <div class="px-6 py-3 text-xs text-slate-500 space-y-1">
        <p>· <b>原始备份</b>（activate）：功能激活时自动创建，永久保留；<b>自动备份</b>（auto）按设定间隔滚动；<b>手动备份</b>（manual）不清理。</p>
        <p>· 每次编辑/删除/新增前都会自动拍<b>回滚快照</b>（snapshot，折叠区可查看）；恢复前也会自动生成 pre-restore 备份兜底。</p>
      </div>
      <el-table :data="backups" size="small" border class="w-full" max-height="320">
        <el-table-column prop="filename" label="文件名" min-width="260" show-overflow-tooltip />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag :type="kindTagType(row.kind)" size="small" effect="plain">{{ kindLabel(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="180" />
        <el-table-column label="大小" width="110">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="confirmRestore(row)">恢复到此</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="px-6 py-2">
        <el-collapse>
          <el-collapse-item :title="`回滚点（写前快照，保留最近 ${snapshotCount} 份）`">
            <el-table :data="snapshots" size="small" border class="w-full" max-height="240">
              <el-table-column prop="filename" label="文件名" min-width="260" show-overflow-tooltip />
              <el-table-column prop="created_at" label="时间" width="180" />
              <el-table-column label="操作" width="90">
                <template #default="{ row }">
                  <el-button link type="danger" size="small" @click="confirmRestore(row)">回滚到此</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </div>
    </section>

    <!-- 编辑对话框 -->
    <el-dialog v-model="editDialogVisible" :title="editingId != null ? `编辑行 ${editLabel}` : '新增行'" width="720px" destroy-on-close>
      <el-form label-position="top" class="grid grid-cols-1 md:grid-cols-2 gap-x-6">
        <el-form-item
          v-for="col in editColumns"
          :key="col.name"
          :label="col.name + (col.pk ? '（主键）' : '')"
          :class="col.type.includes('TEXT') || col.type.includes('JSON') ? 'md:col-span-2' : ''"
        >
          <el-switch
            v-if="isBoolColumn(col)"
            v-model="editForm[col.name]"
          />
          <el-input-number
            v-else-if="isIntColumn(col)"
            v-model="editForm[col.name]"
            :controls="false"
            class="!w-full"
          />
          <el-input
            v-else-if="col.type.includes('TEXT') || col.type.includes('JSON') || isLongValue(editForm[col.name])"
            v-model="editForm[col.name]"
            type="textarea"
            :rows="4"
          />
          <el-input v-else v-model="editForm[col.name]" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingRow" @click="saveRow">保存</el-button>
      </template>
    </el-dialog>

    <!-- 恢复选择对话框 -->
    <el-dialog v-model="restoreDialogVisible" title="一键恢复" width="760px" destroy-on-close>
      <el-alert type="warning" :closable="false" class="mb-3" show-icon
                title="恢复会用所选备份整文件替换当前数据库。执行前系统会自动生成 pre-restore 备份；如 Kikoeru 正在写入可能失败，建议先停止 Kikoeru。" />
      <el-table :data="restoreCandidates" size="small" border max-height="380" highlight-current-row @current-change="r => (restoreSelection = r)">
        <el-table-column label="" width="50">
          <template #default="{ row }">
            <el-radio :model-value="restoreSelection?.filename" :value="row.filename"><span /></el-radio>
          </template>
        </el-table-column>
        <el-table-column prop="filename" label="文件名" min-width="240" show-overflow-tooltip />
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag :type="kindTagType(row.kind)" size="small" effect="plain">{{ kindLabel(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="时间" width="170" />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="restoreDialogVisible = false">取消</el-button>
        <el-button type="danger" :disabled="!restoreSelection" :loading="restoring" @click="doRestore">
          恢复到此备份
        </el-button>
      </template>
    </el-dialog>

    <!-- 一键套用命名向导 -->
    <el-dialog v-model="renameWizardVisible" title="一键套用文件命名" width="980px" destroy-on-close>
      <el-alert type="info" :closable="false" class="mb-3" show-icon
                title="把 t_work.title 重命名为文件夹名中识别出的标题，并置 is_custom_meta=1 防止 Kikoeru 重新抓取时覆盖。" />
      <div class="mb-2 flex flex-wrap items-center gap-2">
        <span class="text-sm text-slate-600">识别方式：</span>
        <el-radio-group v-model="renameMode" size="small">
          <el-radio-button label="template">按重命名模板</el-radio-button>
          <el-radio-button label="regex">自定义正则</el-radio-button>
        </el-radio-group>
        <el-input
          v-if="renameMode === 'regex'"
          v-model="renameRegex"
          class="flex-1"
          style="min-width: 320px"
          size="small"
          placeholder="正则表达式，首个参与匹配的捕获组作为新标题，如 ^RJ\d+\s+(.+)$"
          clearable
          @keyup.enter="loadRenamePreview"
        />
      </div>
      <div v-if="renameMode === 'regex'" class="mb-3 text-xs text-slate-500 leading-5">
        用法：正则匹配文件夹名后，标题取<b>首个参与匹配的捕获组</b>（多分支正则中未参与匹配的分支自动跳过）；不写括号则用整个匹配结果；不匹配的行自动跳过。
        注意从网页复制正则时 <code>\[</code> 可能被渲染成 <code>$$</code>，建议直接用下面的「插入示例」按钮。
        <div class="mt-1 flex flex-wrap gap-2">
          <el-button size="small" @click="insertRegexExample('multi')">插入示例（保留 [社团]/(CV) 段）</el-button>
          <el-button size="small" @click="insertRegexExample('simple')">插入示例（RJ号后取整段）</el-button>
        </div>
      </div>
      <div v-if="renamePreview" class="mb-3 text-sm text-slate-600">
        共 <b>{{ renamePreview.total }}</b> 行：可改名 <b class="text-emerald-600">{{ renamePreview.changed }}</b>，
        跳过 <b class="text-amber-600">{{ renamePreview.skipped }}</b>（{{ renameMode === 'regex' ? '不匹配正则/捕获为空/无变化' : '未按模板命名/无 RJ 号/无变化' }}）
      </div>
      <el-table v-if="renamePreview" :data="renamePreview.items" size="small" border max-height="420">
        <el-table-column prop="id" label="RJ/VJ" width="120" />
        <el-table-column prop="dir" label="文件夹名" min-width="220" show-overflow-tooltip />
        <el-table-column prop="old_title" label="当前标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="新标题" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="row.changed ? 'text-emerald-600 font-medium' : 'text-slate-400'">
              {{ row.changed ? row.new_title : '（不变/跳过）' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.changed" type="success" size="small" effect="plain">改名</el-tag>
            <el-tag v-else type="warning" size="small" effect="plain">跳过</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="renamePreview && renamePreview.items.length > previewLimit" class="mt-2 text-xs text-slate-400">
        仅显示前 {{ previewLimit }} 行预览，执行时按全部 {{ scopeLabel }} 处理。
      </div>
      <template #footer>
        <el-button @click="renameWizardVisible = false">取消</el-button>
        <el-button :loading="renamePreviewing" @click="loadRenamePreview">刷新预览</el-button>
        <el-button
          type="primary"
          :disabled="!renamePreview || renamePreview.changed === 0"
          :loading="renameApplying"
          @click="doApplyRename"
        >
          执行（{{ renamePreview?.changed || 0 }} 行）
        </el-button>
      </template>
    </el-dialog>

    <!-- 评分修复向导（两段式：名单确认 → 后台处理） -->
    <el-dialog v-model="ratingFixVisible" title="评分修复" width="1000px" destroy-on-close @closed="stopRunPolling">
      <el-alert type="info" :closable="false" class="mb-3" show-icon
                title="第一步只列出待处理名单（不访问网络）。你确认后点「开始处理」，才逐个查询 DLsite（本体重抓 → 满分用日文原版校验 → 无评分时套用关联版本评分，优先日文原版），每 40 行一批写入并全程显示进度，可随时取消。写入若遇 Kikoeru 占用会自动重试 3 次；仍失败的行会标为「写入失败」，重跑评分修复即可补上。" />

      <!-- 第一段：待处理名单（零网络请求） -->
      <template v-if="!ratingFixRunStarted">
        <div v-if="ratingFixPreview" class="mb-3 text-sm text-slate-600">
          共 <b>{{ ratingFixPreview.total }}</b> 行待处理：
          0 分 <b class="text-amber-600">{{ ratingFixPreview.zero }}</b>，
          满分复核 <b class="text-amber-600">{{ ratingFixPreview.perfect }}</b>
        </div>
        <el-empty
          v-if="ratingFixPreview && ratingFixPreview.total === 0"
          description="未找到评分为 0 或满分 5 分的作品——已修复过的作品不会再出现（幂等）。"
        />
        <el-table v-else-if="ratingFixPreview" :data="ratingFixPreview.targets" size="small" border max-height="420">
          <el-table-column prop="id" label="RJ/VJ" width="120" />
          <el-table-column label="目标" width="100">
            <template #default="{ row }">
              <el-tag :type="row.target_kind === 'perfect' ? 'warning' : 'info'" size="small" effect="plain">
                {{ row.target_kind === 'perfect' ? '满分复核' : '0 分' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
          <el-table-column prop="dir" label="文件夹名" min-width="220" show-overflow-tooltip />
          <el-table-column label="当前评分" width="100">
            <template #default="{ row }">{{ row.rate_average_2dp ?? 'NULL' }}</template>
          </el-table-column>
        </el-table>
        <div class="mt-3">
          <el-input-number v-model="ratingFixLimit" :min="10" :max="2000" :step="50" size="small" />
          <span class="ml-2 text-xs text-slate-500">单次处理上限（0 分/满分作品较多时按 id 顺序分批）</span>
        </div>
      </template>

      <!-- 第二段：处理进度与结果 -->
      <template v-else>
        <div class="mb-3">
          <el-progress
            :percentage="ratingFixRunPercent"
            :status="runPhase === 'failed' ? 'exception' : (runPhase === 'done' ? 'success' : undefined)"
          />
          <div class="mt-2 text-sm text-slate-600">
            阶段：<b>{{ runPhaseLabel }}</b>
            已处理 <b>{{ ratingFixRunStatus.done }}</b> / {{ ratingFixRunStatus.total }}：
            套用 <b class="text-emerald-600">{{ ratingFixRunStatus.applied }}</b>，
            无评分 <b class="text-slate-500">{{ ratingFixRunStatus.none }}</b>，
            <template v-if="ratingFixRunStatus.error > 0">
              <b class="text-red-500">网络失败 {{ ratingFixRunStatus.error }}</b>（下次重跑自动重试），
            </template>
            <template v-if="ratingFixRunStatus.write_failed > 0">
              <b class="text-red-500">写入失败 {{ ratingFixRunStatus.write_failed }}</b>（重跑评分修复可补上），
            </template>
            <span v-if="ratingFixRunStatus.finished_at" class="ml-1 text-xs text-slate-400">完成于 {{ ratingFixRunStatus.finished_at }}</span>
          </div>
        </div>
        <el-table v-if="ratingFixRunStatus.results.length" :data="ratingFixRunStatus.results" size="small" border max-height="380">
          <el-table-column prop="id" label="RJ/VJ" width="110" />
          <el-table-column prop="title" label="标题" min-width="180" show-overflow-tooltip />
          <el-table-column label="当前" width="80">
            <template #default="{ row }">{{ row.current_rate_average_2dp ?? 'NULL' }}</template>
          </el-table-column>
          <el-table-column label="来源" width="190">
            <template #default="{ row }">
              <template v-if="row.plan === 'own'">
                <el-tag type="success" size="small" effect="plain">本体重抓</el-tag>
              </template>
              <template v-else-if="row.plan === 'linked'">
                <el-tag type="primary" size="small" effect="plain">{{ row.source_lang || '其他版本' }}</el-tag>
                <span class="ml-1 text-xs">{{ row.source_rjcode }}</span>
              </template>
              <template v-else-if="row.plan === 'error'">
                <el-tag type="danger" size="small" effect="plain">网络失败</el-tag>
              </template>
              <span v-else class="text-xs text-slate-400">-</span>
            </template>
          </el-table-column>
          <el-table-column label="新评分" width="120">
            <template #default="{ row }">
              <span v-if="row.applied" class="text-emerald-600 font-medium">
                {{ row.new_rate_average_2dp }}（{{ row.new_rate_count }} 评）
              </span>
              <span v-else-if="row.write_error" class="text-red-500 text-xs">写入失败</span>
              <span v-else-if="row.plan === 'error'" class="text-red-400 text-xs">未写入</span>
              <span v-else class="text-slate-400 text-xs">跳过</span>
            </template>
          </el-table-column>
          <el-table-column min-width="160" show-overflow-tooltip>
            <template #header>说明</template>
            <template #default="{ row }">{{ row.write_error || row.reason }}</template>
          </el-table-column>
        </el-table>
      </template>

      <template #footer>
        <template v-if="!ratingFixRunStarted">
          <el-button @click="ratingFixVisible = false">取消</el-button>
          <el-button :loading="ratingFixPreviewing" @click="loadRatingFixPreview">刷新名单</el-button>
          <el-button
            type="primary"
            :disabled="!ratingFixPreview || ratingFixPreview.total === 0"
            @click="startRatingFixRun"
          >
            开始处理（{{ ratingFixPreview?.total || 0 }} 行）
          </el-button>
        </template>
        <template v-else>
          <el-button v-if="runPhase === 'running'" type="danger" plain @click="cancelRatingFixRun">取消任务</el-button>
          <el-button type="primary" @click="ratingFixVisible = false">
            {{ runPhase === 'running' ? '后台运行，关闭窗口' : '关闭' }}
          </el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 检测结果对话框 -->
    <el-dialog v-model="diagnoseDialogVisible" title="数据库检测结果" width="640px">
      <div class="space-y-3">
        <div v-for="(s, i) in diagnoseResult?.steps || []" :key="i" class="flex items-start gap-2">
          <CheckCircle2 v-if="s.ok" :size="16" class="text-emerald-500 mt-0.5 shrink-0" />
          <XCircle v-else :size="16" class="text-red-500 mt-0.5 shrink-0" />
          <div>
            <div class="text-sm font-medium text-slate-800">{{ s.step }}</div>
            <div class="text-xs text-slate-500 break-all">{{ s.detail }}</div>
          </div>
        </div>
      </div>
    </el-dialog>

    <!-- 增量整理对话框 -->
    <el-dialog v-model="cleanupDialogVisible" title="增量整理（title 替换 + 评分异常修复）" width="640px" destroy-on-close>
      <div class="space-y-3">
        <div class="text-sm text-slate-600 space-y-1">
          <p>① <b>title 反解同步</b>：按你在设置中配置的重命名模板反解文件夹名，只取 <b>work_name</b> 段写入数据库 title（如模板 <code>[{'{'}rjcode{'}'}][{'{'}work_name{'}'}]</code> 把 <code>[RJ01630673][作品名]</code> 反解为 <code>作品名</code>）；</p>
          <p>② <b>评分修复</b>：仅 <b>0 分 / 无评分 / 满分（≥5）</b>的异常作品触发修复（关联版本日文原版优先 + 满分核验），正常评分不碰。</p>
        </div>
        <el-checkbox v-model="cleanupStrictTemplate">
          仅按我的重命名模板严格反解（推荐）——不匹配的文件夹保持原 title，不做启发式猜测
        </el-checkbox>
        <div class="text-xs text-slate-400">
          当前模板：<code>{{ renameTemplate || '（未配置，将无法反解）' }}</code>
          <span v-if="!cleanupStrictTemplate"> · 已关闭严格模式：不符合模板的历史文件夹会用通用形态（<code>[RJ..][名字]</code> 等）兜底解析</span>
        </div>
        <div class="text-xs text-slate-400">起点按 <b>加入时间（created_at）</b> 判定——不是发售顺序；t_work.id 是 DLsite 作品号，方向对应发售时间，不用于此处。</div>
        <el-input
          v-model="cleanupSinceInput"
          placeholder="起点：留空 = 从上次处理位置继续；填 0 = 整个数据库；填 RJ 号（如 RJ01649167）= 处理该作品之后入库的数据（不含它）"
          clearable
        />
        <div v-if="cleanupCursorInfo" class="text-xs text-slate-500">
          上次整理到：<b>{{ cleanupCursorInfo.rjcode }}</b>
          <span v-if="cleanupCursorInfo.finished_at">（{{ cleanupCursorInfo.finished_at }}，处理 {{ cleanupCursorInfo.processed_count }} 个）</span>
          —— 留空起点即从此之后继续
        </div>
        <div v-if="cleanupStatusData.running" class="rounded-xl border border-slate-200 bg-slate-50 p-3 space-y-2">
          <div class="flex items-center justify-between text-sm">
            <span class="font-medium text-slate-700">
              {{ cleanupStatusData.phase === 'title' ? '① title 替换中' : (cleanupStatusData.phase === 'rating' ? '② 评分异常修复中' : '整理进行中') }}
            </span>
            <el-button size="small" type="danger" plain @click="cancelCleanup">取消</el-button>
          </div>
          <div v-if="cleanupStatusData.phase === 'title'" class="text-xs text-slate-500">
            title 进度：{{ cleanupStatusData.title_done }} / {{ cleanupStatusData.title_total }}
            （替换 {{ cleanupStatusData.title_updated }} · 一致 {{ cleanupStatusData.title_unchanged }} · 未匹配 {{ cleanupStatusData.title_missed }}）
          </div>
          <div v-if="cleanupStatusData.phase === 'rating'" class="text-xs text-slate-500">
            评分修复：{{ cleanupStatusData.rating_phase }} {{ cleanupStatusData.rating_done }} / {{ cleanupStatusData.rating_total }}
            （已应用 {{ cleanupStatusData.rating_applied || 0 }}）
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="cleanupDialogVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :disabled="!featureEnabled || cleanupStarting || cleanupStatusData.running"
          :loading="cleanupStarting"
          @click="startCleanup"
        >
          开始整理
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CheckCircle2,
  Database,
  FolderSync,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Search,
  Star,
  Stethoscope,
  Trash2,
  Undo2,
  Wand2,
  XCircle
} from 'lucide-vue-next'
import { configApi, kikoeruDbApi } from '../api'
import AppPageHeader from '../components/common/AppPageHeader.vue'

// ---- 状态 ----
const tables = ref([])
const activeTable = ref('')
const rows = ref([])
const columns = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const searchText = ref('')
const loadingRows = ref(false)
const selectedRows = ref([])

const dbPath = ref('')
const featureEnabled = ref(false)
const scanStatus = ref({ listening: false, last_scan_finished_at: '', checkpoint: '' })

// ---- 增量整理（title 反解 + 评分异常修复） ----
const cleanupDialogVisible = ref(false)
const cleanupStarting = ref(false)
const cleanupSinceInput = ref('')
const cleanupCursorInfo = ref(null)
const cleanupStrictTemplate = ref(true)
const renameTemplate = ref('')
const cleanupStatusData = ref({ running: false, phase: 'idle', title_done: 0, title_total: 0, title_updated: 0, title_unchanged: 0, title_missed: 0, rating_total: 0, rating_done: 0, rating_applied: 0, rating_phase: 'pending', since_desc: '', finished_at: '' })
let cleanupPollTimer = null

async function openCleanupDialog () {
  cleanupDialogVisible.value = true
  try {
    const cursor = await kikoeruDbApi.cleanupCursor()
    cleanupCursorInfo.value = cursor && cursor.rjcode ? cursor : null
    // 默认起点：上次处理位置之后继续（输入框留空即从游标继续）
    cleanupSinceInput.value = ''
  } catch {
    cleanupCursorInfo.value = null
  }
  loadRenameTemplate()
  pollCleanupStatus()
}

async function loadRenameTemplate () {
  try {
    const config = await configApi.get()
    renameTemplate.value = String(config?.rename?.template || '')
  } catch {
    renameTemplate.value = ''
  }
}

async function startCleanup () {
  const since = cleanupSinceInput.value.trim()
  if (cleanupStarting.value) return
  const desc = since === '0'
    ? '整个数据库'
    : (since ? `起点 ${since} 之后入库的所有作品（不含起点）` : `上次处理位置${cleanupCursorInfo.value ? `（${cleanupCursorInfo.value.rjcode}）之后` : '（数据库起始）'}之后入库的所有作品`)
  try {
    await ElMessageBox.confirm(
      `将对 ${desc} 执行：\n① title 替换（DLsite 官方名称校验/覆盖）；\n② 评分修复（仅 0 分 / 无评分 / 满分异常作品，正常评分不碰）。\n\n继续？`,
      '确认增量整理',
      { confirmButtonText: '开始整理', cancelButtonText: '取消', type: 'info' }
    )
  } catch {
    return
  }
  cleanupStarting.value = true
  try {
    const result = await kikoeruDbApi.cleanupStart({
      since,
      strict_template: cleanupStrictTemplate.value
    })
    if (result?.started) {
      ElMessage.success(`整理任务已启动（${result.since_desc || desc}），目标 ${result.total ?? '?'} 个作品，其中评分异常 ${result.abnormal_rating ?? 0} 个`)
      cleanupDialogVisible.value = false
      pollCleanupStatus()
    } else {
      ElMessage.warning(result?.reason || '任务未能启动')
    }
  } catch (error) {
    ElMessage.error('启动失败: ' + (error.response?.data?.detail || error.message || '未知错误'))
  } finally {
    cleanupStarting.value = false
  }
}

function pollCleanupStatus () {
  if (cleanupPollTimer) clearTimeout(cleanupPollTimer)
  fetchCleanupStatus()
  cleanupPollTimer = setTimeout(pollCleanupStatus, 2500)
}

async function fetchCleanupStatus () {
  try {
    const data = await kikoeruDbApi.cleanupStatus()
    cleanupStatusData.value = data
    if (data?.running) {
      if (!cleanupPollTimer) cleanupPollTimer = setTimeout(pollCleanupStatus, 2500)
    } else if (cleanupPollTimer) {
      clearTimeout(cleanupPollTimer)
      cleanupPollTimer = null
    }
  } catch {
    /* 状态轮询失败静默 */
  }
}

async function cancelCleanup () {
  try {
    await kikoeruDbApi.cleanupCancel()
    ElMessage.info('已发送取消请求（当前阶段完成后停止）')
  } catch (error) {
    ElMessage.error('取消失败: ' + (error.response?.data?.detail || error.message || '未知错误'))
  }
}

onMounted(() => pollCleanupStatus())

const backups = ref([])
const snapshots = ref([])
const backingUp = ref(false)
const restoreDialogVisible = ref(false)
const restoreSelection = ref(null)
const restoring = ref(false)

const editDialogVisible = ref(false)
const editingRow = ref(null)
const editForm = ref({})
const savingRow = ref(false)

const renameWizardVisible = ref(false)
const renamePreview = ref(null)
const renamePreviewing = ref(false)
const renameMode = ref('template')
const renameRegex = ref('')
watch(renameMode, () => {
  if (renameWizardVisible.value) loadRenamePreview()
})

// 示例正则由程序直接填入输入框——从网页/聊天复制时 \[ \] 会被 LaTeX 渲染转成 $$
// 注意：编号用 \d{6,8}(?!\d) 贪婪取最长并防截断；写成 (?:\d{6,8}) 有序分支
// 会先匹配 6 位，8 位号的剩余数字会被卷进标题（v2.6.8 用户实测）
const REGEX_EXAMPLES = {
  multi: '\\b\\[?RJ\\d{6,8}(?!\\d)\\]?\\s*(?:\\[([^\\[\\]]+)\\]\\s*$|(.+))',
  simple: '\\b\\[?RJ\\d{6,8}(?!\\d)\\]?\\s*(.+)$'
}
function insertRegexExample(key) {
  renameRegex.value = REGEX_EXAMPLES[key] || ''
  loadRenamePreview()
}
const renameApplying = ref(false)
const renameScopedToSelection = ref(false)
const previewLimit = 300

// ---- 评分修复（两段式：名单零请求，确认后后台处理） ----
const ratingFixVisible = ref(false)
const ratingFixPreview = ref(null)
const ratingFixPreviewing = ref(false)
const ratingFixScopedToSelection = ref(false)
const ratingFixLimit = ref(300)
const ratingFixRunStarted = ref(false)
const ratingFixRunStatus = ref({ running: false, phase: 'idle', total: 0, done: 0, applied: 0, none: 0, error: 0, write_failed: 0, results: [] })
const ratingFixPollTimer = ref(null)

const runPhase = computed(() => {
  const s = ratingFixRunStatus.value
  if (!ratingFixRunStarted.value) return 'idle'
  if (s.phase === 'failed') return 'failed'
  if (s.running) return 'running'
  return 'done'
})
const runPhaseLabel = computed(() => ({
  idle: '',
  running: ratingFixRunStatus.value.phase === 'writing' ? '写入数据库中' : '抓取 DLsite 元数据中',
  done: '已完成',
  failed: '任务异常'
}[runPhase.value] || ''))
const ratingFixRunPercent = computed(() => {
  const s = ratingFixRunStatus.value
  if (!s.total) return 0
  return Math.min(100, Math.round((s.done / s.total) * 100))
})

async function openRatingFixWizard(scoped) {
  ratingFixScopedToSelection.value = scoped
  ratingFixRunStarted.value = false
  // 若后台任务仍在跑，直接进入进度视图
  try {
    const status = await kikoeruDbApi.ratingFixRunStatus()
    if (status.running) {
      ratingFixRunStarted.value = true
      ratingFixRunStatus.value = status
      ratingFixVisible.value = true
      startRunPolling()
      return
    }
  } catch { /* 忽略，走名单流程 */ }
  ratingFixVisible.value = true
  await loadRatingFixPreview()
}

async function loadRatingFixPreview() {
  ratingFixPreviewing.value = true
  try {
    let ids = null
    if (ratingFixScopedToSelection.value) {
      ids = selectedRows.value.map(r => r.id).filter(v => v !== undefined)
      if (!ids.length) {
        ElMessage.warning('当前选择不支持按行处理（无 id 列），改为全库 0 分/满分名单')
        ratingFixScopedToSelection.value = false
      }
    }
    ratingFixPreview.value = await kikoeruDbApi.ratingFixPreview({
      ids: ratingFixScopedToSelection.value ? ids : null,
      limit: ratingFixLimit.value
    })
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '获取待处理名单失败'))
  } finally {
    ratingFixPreviewing.value = false
  }
}

async function startRatingFixRun() {
  const total = ratingFixPreview.value?.total || 0
  try {
    await ElMessageBox.confirm(
      `确定开始处理 ${total} 行？处理阶段才会访问 DLsite，每 40 行一批写入（自动生成回滚快照），可随时取消。`,
      '开始处理',
      { type: 'warning', confirmButtonText: '开始处理', cancelButtonText: '再想想' }
    )
  } catch {
    return
  }
  try {
    let ids = null
    if (ratingFixScopedToSelection.value) {
      ids = selectedRows.value.map(r => r.id).filter(v => v !== undefined)
    }
    const res = await kikoeruDbApi.ratingFixRun({ ids, limit: ratingFixLimit.value })
    if (!res.started) {
      ElMessage.warning(res.reason || '任务未能启动')
      return
    }
    ratingFixRunStarted.value = true
    ratingFixRunStatus.value = {
      running: true, phase: 'fetching', total,
      done: 0, applied: 0, none: 0, error: 0, write_failed: 0, results: []
    }
    startRunPolling()
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '任务启动失败'))
  }
}

function startRunPolling() {
  stopRunPolling()
  ratingFixPollTimer.value = setInterval(pollRunStatus, 1500)
}

function stopRunPolling() {
  if (ratingFixPollTimer.value) {
    clearInterval(ratingFixPollTimer.value)
    ratingFixPollTimer.value = null
  }
}

async function pollRunStatus() {
  try {
    const s = await kikoeruDbApi.ratingFixRunStatus()
    ratingFixRunStatus.value = s
    if (!s.running) {
      stopRunPolling()
      if (s.phase === 'failed') {
        ElMessage.error(`评分修复任务异常：${s.error_detail || '未知错误'}`)
      } else {
        const parts = [`套用 ${s.applied} 行`, `无评分 ${s.none} 行`, `失败 ${s.error} 行`]
        if (s.write_failed > 0) parts.push(`写入失败 ${s.write_failed} 行（重跑评分修复可补上）`)
        ElMessage.success(`评分修复结束：${parts.join('，')}`)
      }
      await Promise.all([loadRows(), loadBackups()])
    }
  } catch { /* 轮询失败静默，下轮再试 */ }
}

async function cancelRatingFixRun() {
  try {
    await kikoeruDbApi.ratingFixCancel()
    ElMessage.info('已发送取消请求，正在等待当前作品处理完成')
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '取消失败'))
  }
}

const diagnosing = ref(false)
const diagnoseDialogVisible = ref(false)
const diagnoseResult = ref(null)

// ---- 计算属性 ----
const currentMode = computed(() => tables.value.find(t => t.name === activeTable.value)?.mode || 'readonly')
const displayColumns = computed(() => columns.value.slice(0, 30))
const editColumns = computed(() => columns.value.filter(c => !c.pk || editingRow.value == null))
const editLabel = computed(() => {
  const row = editingRow.value
  if (!row) return ''
  return row.id ?? JSON.stringify(row)
})
const scopeLabel = computed(() => (renameScopedToSelection.value ? `已选 ${renamePreview.value?.total || 0} 行` : '全库'))
const restoreCandidates = computed(() => [...backups.value, ...snapshots.value])
const snapshotCount = computed(() => snapshots.value.length)

// ---- 工具 ----
function renderCell(value) {
  if (value === null || value === undefined) return 'NULL'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text
}

function isLongValue(value) {
  return typeof value === 'string' && value.length > 80
}

function isBoolColumn(col) {
  return /BOOL|TINYINT\(1\)/i.test(col.type || '')
}

function isIntColumn(col) {
  return /INT|BIGINT/i.test(col.type || '') && !/BOOL/i.test(col.type || '')
}

function columnWidth(col) {
  const t = (col.type || '').toUpperCase()
  if (t.includes('TEXT') || t.includes('JSON')) return 260
  if (t.includes('DATETIME')) return 170
  if (/INT|FLOAT|BOOL/.test(t)) return 110
  return 180
}

function kindLabel(kind) {
  return { activate: '原始备份', auto: '自动备份', manual: '手动备份', snapshot: '回滚快照', 'pre-restore': '恢复前备份' }[kind] || kind
}

function kindTagType(kind) {
  return { activate: 'primary', auto: 'success', manual: '', snapshot: 'warning', 'pre-restore': 'info' }[kind] || 'info'
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function apiErrorDetail(error, fallback) {
  return error?.response?.data?.detail || error?.message || fallback
}

// ---- 数据加载 ----
async function loadConfig() {
  try {
    const config = await configApi.get()
    dbPath.value = config?.kikoeru_db?.db_path || ''
    featureEnabled.value = Boolean(config?.kikoeru_db?.enabled)
  } catch (error) {
    console.warn('[KikoeruDb] 配置读取失败', error)
  }
}

async function loadScanStatus() {
  try {
    scanStatus.value = await kikoeruDbApi.scanStatus()
  } catch {
    scanStatus.value = { listening: false, last_scan_finished_at: '' }
  }
}

async function loadTables() {
  try {
    const data = await kikoeruDbApi.tables()
    tables.value = data.tables || []
    if (!activeTable.value && tables.value.length > 0) {
      activeTable.value = tables.value[0].name
    }
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '读取表清单失败'))
  }
}

async function loadRows() {
  if (!activeTable.value) return
  loadingRows.value = true
  try {
    const data = await kikoeruDbApi.rows(activeTable.value, {
      page: page.value,
      size: pageSize.value,
      search: searchText.value,
      sort: listSort.value.field ? `${listSort.value.field}:${listSort.value.dir}` : ''
    })
    columns.value = data.columns || []
    rows.value = data.rows || []
    total.value = data.total || 0
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '读取数据失败'))
  } finally {
    loadingRows.value = false
  }
}

// 列头排序（服务端排序：分页在服务端，必须回服务端排）
const rowsTableRef = ref(null)
const listSort = ref({ field: '', dir: '' })

function onSortChange({ prop, order }) {
  listSort.value = order
    ? { field: prop, dir: order === 'ascending' ? 'asc' : 'desc' }
    : { field: '', dir: '' }
  page.value = 1
  loadRows()
}

function resetTableSort() {
  listSort.value = { field: '', dir: '' }
  nextTick(() => {
    rowsTableRef.value?.clearSort?.()
  })
}

async function loadBackups() {
  try {
    const data = await kikoeruDbApi.backups()
    backups.value = (data.backups || []).filter(b => b.kind !== 'snapshot')
    const snapData = await kikoeruDbApi.snapshots()
    snapshots.value = snapData.snapshots || []
  } catch {
    /* 未启用时静默 */
  }
}

function onSelectionChange(selection) {
  selectedRows.value = selection
}

async function refreshAll() {
  await Promise.all([loadConfig(), loadScanStatus(), loadTables(), loadBackups()])
  await loadRows()
}

// ---- 编辑 / 新增 / 删除 ----
function openEditDialog(row) {
  editingRow.value = row
  editForm.value = {}
  for (const col of columns.value) {
    let value = row[col.name]
    if (value !== null && typeof value === 'object') value = JSON.stringify(value)
    editForm.value[col.name] = value
  }
  editDialogVisible.value = true
}

function openCreateDialog() {
  editingRow.value = null
  editForm.value = {}
  for (const col of columns.value) {
    if (col.pk && col.name === 'id') continue // 自增主键不填
    editForm.value[col.name] = isBoolColumn(col) ? false : null
  }
  editDialogVisible.value = true
}

function parseMaybeJson(col, value) {
  if (value === '' || value === null || value === undefined) return null
  if (/JSON/i.test(col.type || '')) {
    // sqlite 无原生 JSON 类型：Kikoeru 的 memo 等列就是 TEXT 存 JSON 字符串，
    // 这里只做合法性校验后原样绑定字符串（parse 成 dict 会导致 sqlite 绑定报错）
    try {
      JSON.parse(value)
    } catch {
      throw new Error(`字段 ${col.name} 不是合法 JSON，请修正后再保存`)
    }
    return value
  }
  return value
}

async function saveRow() {
  savingRow.value = true
  try {
    const patch = {}
    for (const col of columns.value) {
      const raw = editForm.value[col.name]
      const value = parseMaybeJson(col, raw)
      if (editingRow.value) {
        const before = editingRow.value[col.name] ?? null
        if (JSON.stringify(value) === JSON.stringify(before)) continue
        patch[col.name] = value
      } else if (value !== null && value !== undefined) {
        patch[col.name] = value
      }
    }
    if (editingRow.value) {
      if (Object.keys(patch).length === 0) {
        ElMessage.info('没有修改任何字段')
        editDialogVisible.value = false
        return
      }
      const id = columns.value.filter(c => c.pk).length > 1
        ? Object.fromEntries(columns.value.filter(c => c.pk).map(c => [c.name, editingRow.value[c.name]]))
        : editingRow.value[columns.value.find(c => c.pk)?.name || 'rowid']
      const result = await kikoeruDbApi.updateRow(activeTable.value, id, patch)
      ElMessage.success(
        result.channel === 'api'
          ? '已通过 Kikoeru API 写入'
          : '已直接写入数据库（已自动生成回滚快照）'
      )
    } else {
      await kikoeruDbApi.insertRow(activeTable.value, patch)
      ElMessage.success('新增成功')
    }
    editDialogVisible.value = false
    await loadRows()
    await loadBackups()
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '保存失败'))
  } finally {
    savingRow.value = false
  }
}

async function deleteSelected() {
  if (selectedRows.value.length === 0) return
  const cacheHint = activeTable.value === 't_work'
    ? ' 注意：Kikoeru 音频缓存按作品归档且不再自动淘汰，删除作品不会清理已缓存文件。'
    : ''
  try {
    await ElMessageBox.confirm(
      `确定删除选中的 ${selectedRows.value.length} 行？删除前会自动生成回滚快照。${cacheHint}`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  const pkCols = columns.value.filter(c => c.pk)
  let ok = 0
  let relatedTracks = 0
  let cacheNote = ''
  const cascadedCounts = {}
  for (const row of selectedRows.value) {
    try {
      const id = pkCols.length > 1
        ? Object.fromEntries(pkCols.map(c => [c.name, row[c.name]]))
        : row[pkCols[0]?.name || 'rowid']
      const result = await kikoeruDbApi.deleteRow(activeTable.value, id)
      ok += 1
      relatedTracks += Number(result?.related_tracks || 0)
      for (const item of result?.cascaded || []) {
        cascadedCounts[item.table] = (cascadedCounts[item.table] || 0) + Number(item.deleted || 0)
      }
      if (result?.cache_note) cacheNote = result.cache_note
    } catch (error) {
      ElMessage.error(apiErrorDetail(error, `删除行失败: ${JSON.stringify(row).slice(0, 60)}`))
    }
  }
  if (ok > 0) {
    let message = `已删除 ${ok} 行`
    if (relatedTracks > 0) message += `；关联 t_track 轨道 ${relatedTracks} 条`
    const cascadedText = Object.entries(cascadedCounts)
      .filter(([, count]) => count > 0)
      .map(([table, count]) => `${table} ${count} 行`)
      .join('、')
    if (cascadedText) message += `；同时清理关联 ${cascadedText}`
    ElMessage.success(message)
    if (cacheNote) ElMessage.warning(cacheNote)
    await loadRows()
    await loadBackups()
  }
}

// ---- 备份 / 恢复 ----
async function doBackup() {
  backingUp.value = true
  try {
    const result = await kikoeruDbApi.createBackup()
    ElMessage.success(`备份完成: ${result.filename}`)
    await loadBackups()
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '备份失败'))
  } finally {
    backingUp.value = false
  }
}

function confirmRestore(backup) {
  restoreSelection.value = backup
  restoreDialogVisible.value = true
}

async function doRestore() {
  if (!restoreSelection.value) return
  try {
    await ElMessageBox.confirm(
      `确定恢复到「${restoreSelection.value.filename}」？当前数据库会先自动备份（pre-restore）。`,
      '恢复确认',
      { type: 'warning', confirmButtonText: '确认恢复', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  restoring.value = true
  try {
    try {
      await kikoeruDbApi.restoreBackup(restoreSelection.value.filename)
    } catch (error) {
      if (error?.response?.status !== 409) throw error
      // 跨版本护栏：备份缺少当前库已有的表（如新版 t_track），需二次确认
      try {
        await ElMessageBox.confirm(
          `${error.response?.data?.detail || '该备份缺少当前库已有的表。'} 继续恢复会回退到旧结构，相关历史数据需要 Kikoeru 重新扫描重建。`,
          '跨版本恢复确认',
          { type: 'warning', confirmButtonText: '仍然恢复', cancelButtonText: '取消' }
        )
      } catch {
        return
      }
      await kikoeruDbApi.restoreBackup(restoreSelection.value.filename, true)
    }
    ElMessage.success('恢复完成')
    restoreDialogVisible.value = false
    await Promise.all([loadBackups(), loadRows()])
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '恢复失败'))
  } finally {
    restoring.value = false
  }
}

// ---- 一键套用命名 ----
async function openRenameWizard(scoped) {
  renameScopedToSelection.value = scoped
  renameWizardVisible.value = true
  await loadRenamePreview()
}

async function loadRenamePreview() {
  renamePreviewing.value = true
  try {
    const ids = renameScopedToSelection.value
      ? selectedRows.value.map(r => r.id).filter(v => v !== undefined)
      : null
    if (renameScopedToSelection.value && (!ids || ids.length === 0)) {
      ElMessage.warning('当前表不支持按行选择（无 id 列），改为全库预览')
      renameScopedToSelection.value = false
    }
    renamePreview.value = await kikoeruDbApi.renamePreview({
      ids: renameScopedToSelection.value ? ids : null,
      mode: renameMode.value,
      regex: renameRegex.value
    })
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '生成预览失败'))
  } finally {
    renamePreviewing.value = false
  }
}

async function doApplyRename() {
  try {
    await ElMessageBox.confirm(
      `确定对 ${scopeLabel.value} 中 ${renamePreview.value.changed} 行执行改名？执行前会自动生成回滚快照。`,
      '执行确认',
      { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  renameApplying.value = true
  try {
    const ids = renameScopedToSelection.value
      ? (renamePreview.value.items || []).filter(i => i.changed).map(i => i.id)
      : null
    const result = await kikoeruDbApi.renameApply({
      ids,
      mode: renameMode.value,
      regex: renameRegex.value
    })
    ElMessage.success(`已套用 ${result.applied} 行（跳过 ${result.skipped}）`)
    renameWizardVisible.value = false
    await Promise.all([loadRows(), loadBackups()])
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '执行失败'))
  } finally {
    renameApplying.value = false
  }
}

// ---- 检测 ----
async function runDiagnose() {
  diagnosing.value = true
  try {
    diagnoseResult.value = await kikoeruDbApi.diagnose()
    diagnoseDialogVisible.value = true
  } catch (error) {
    ElMessage.error(apiErrorDetail(error, '检测失败'))
  } finally {
    diagnosing.value = false
  }
}

// ---- 表切换 ----
watch(activeTable, () => {
  page.value = 1
  searchText.value = ''
  selectedRows.value = []
  resetTableSort()
  loadRows()
})

onMounted(refreshAll)
onActivated(() => {
  loadConfig()
  loadScanStatus()
})
onBeforeUnmount(stopRunPolling)
onBeforeUnmount(() => {
  if (cleanupPollTimer) {
    clearTimeout(cleanupPollTimer)
    cleanupPollTimer = null
  }
})
</script>

<style scoped>
button:not(:disabled) { cursor: pointer; }
button:disabled { cursor: not-allowed; }

/* ==============================================================
 * 页头按钮：page-head-btn 规范（对齐 LibraryBackup.vue / ASMRSync.vue，
 * 该规范样式定义在各页面 scoped 内，新页面需自带一份）
 * ============================================================ */
.page-head-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 36px;
  padding: 0 14px;
  border-radius: 10px;
  border: 1px solid rgba(15, 23, 42, 0.12);
  background: #fff;
  color: #1e293b;
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  transition:
    transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1),
    box-shadow 0.3s cubic-bezier(0.34, 1.56, 0.64, 1),
    background 0.35s ease,
    border-color 0.25s ease,
    color 0.25s ease,
    opacity 0.25s ease;
  will-change: transform, opacity;
}
.page-head-btn :deep(.page-head-btn-icon) {
  flex-shrink: 0;
  transition: transform 0.45s cubic-bezier(0.34, 1.56, 0.64, 1), filter 0.3s ease;
}
.page-head-btn :deep(svg) { flex-shrink: 0; }
.page-head-btn:hover {
  transform: translateY(-2px) scale(1.02);
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.08);
}
.page-head-btn:active:not(:disabled) {
  transform: scale(0.96);
  transition: transform 0.12s ease;
}
.page-head-btn:disabled {
  opacity: 0.7;
  cursor: not-allowed;
}
.page-head-btn.primary {
  background: linear-gradient(135deg, #111827, #1e293b);
  color: #fff;
  border-color: transparent;
  box-shadow: 0 8px 18px rgba(15, 23, 42, 0.18);
}
.page-head-btn.primary:hover {
  background: linear-gradient(135deg, #1e293b, #334155);
  box-shadow: 0 14px 28px rgba(15, 23, 42, 0.28);
}
.page-head-btn.ghost { background-color: #fff; }
.page-head-btn.ghost:hover {
  background-color: #f8fafc;
  border-color: rgba(15, 23, 42, 0.2);
}
/* === Ghost 蓝色变体（评分修复/备份） === */
.page-head-btn.ghost.is-blue {
  color: #1d4ed8;
  border-color: rgba(59, 130, 246, 0.35);
  background: linear-gradient(180deg, #eff6ff 0%, #dbeafe 100%);
}
.page-head-btn.ghost.is-blue:hover {
  background: linear-gradient(180deg, #dbeafe 0%, #bfdbfe 100%);
  border-color: rgba(37, 99, 235, 0.55);
  color: #1e40af;
  box-shadow: 0 10px 22px rgba(37, 99, 235, 0.16);
}
.page-head-btn.icon-only {
  padding: 0;
  width: 36px;
  justify-content: center;
}
.page-head-btn-label {
  display: inline-block;
  text-align: center;
  transition: opacity 0.2s ease, letter-spacing 0.3s ease;
}
.page-head-btn.primary .page-head-btn-label { min-width: 70px; }
.page-head-btn.ghost .page-head-btn-label { min-width: 56px; }
.page-head-btn:hover .page-head-btn-label { letter-spacing: 0.04em; }

.kikoeru-db-page :deep(.el-table .cell) {
  font-variant-numeric: tabular-nums;
}
</style>
