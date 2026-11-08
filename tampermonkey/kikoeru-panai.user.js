// ==UserScript==
// @name              网盘智能识别助手
// @namespace         https://github.com/syhyz1990/panAI
// @version           2.1.0
// @author            YouXiaoHou,52fisher
// @description       智能识别选中文字中的🔗网盘链接和🔑提取码，识别成功打开网盘链接并自动填写提取码，省去手动复制提取码在输入的烦恼。支持识别 ✅百度网盘 ✅阿里云盘 ✅腾讯微云 ✅蓝奏云 ✅天翼云盘 ✅移动云盘 ✅迅雷云盘 ✅123云盘 ✅360云盘 ✅115网盘 ✅奶牛快传 ✅城通网盘 ✅夸克网盘 ✅FlowUs息流 ✅Chrome 扩展商店 ✅Edge 扩展商店 ✅Firefox 扩展商店 ✅Windows 应用商店 ✅Gofile ✅PikPak ✅Transfer.it。附：百度网盘可整理成 KikoeruManager 规范格式并预览编辑；Gofile / PikPak / Transfer.it 可一键发送到 KikoeruManager 的「HTTP 下载」。2.0.3 起「提取：」「解压：」这种省略「码」字的写法也能识别；2.0.5 起按「解压」「提取」字样区分，支持「解压密码是 X」「解压：」换行写值、括号说明截断；2.0.6 起 Gofile / PikPak 不再抓取提取码与解压码，默认留空；2.1.0 起支持 Transfer.it（transfer.it/t/…），行为与 Gofile / PikPak 一致。
// @license           AGPL-3.0-or-later
// @homepage          https://www.youxiaohou.com/tool/install-panai.html
// @supportURL        https://github.com/syhyz1990/panAI
// // @updateURL      https://www.youxiaohou.com/panai.user.js
// // @downloadURL    https://www.youxiaohou.com/panai.user.js
// 上面两行已停用：保留官方更新地址会让上游发版时直接覆盖掉 KikoeruManager 改版。
// @match             *://*/*
// @require           https://unpkg.com/sweetalert2@10.16.6/dist/sweetalert2.min.js
// @require           https://unpkg.com/hotkeys-js@3.13.3/dist/hotkeys.min.js
// @resource          swalStyle https://unpkg.com/sweetalert2@10.16.6/dist/sweetalert2.min.css
// @grant             GM_openInTab
// @grant             GM_setValue
// @grant             GM_getValue
// @grant             GM_registerMenuCommand
// @grant             GM_getResourceText
// @grant             GM_xmlhttpRequest
// @grant             GM_info
// @connect           localhost
// @connect           127.0.0.1
// @connect           *
// @run-at            document-idle
// ==/UserScript==

/* ===========================================================================
 * 使用说明（KikoeruManager 改版）
 * ---------------------------------------------------------------------------
 * 一、安装
 *   1. 浏览器安装 Tampermonkey（油猴）扩展；
 *   2. 打开 KikoeruManager「概览」页 →「插件说明」→ 下载本脚本，或在 Tampermonkey
 *      里新建脚本后粘贴本文件全文保存；
 *   3. 首次运行依赖两个 CDN 资源（sweetalert2 / hotkeys-js），无外网时需自备。
 *
 * 二、配置（必须做一次）
 *   1. 点击浮动图标 → 「识别助手配置」，或从 Tampermonkey 菜单进入设置；
 *   2. 「收件箱地址」填 KikoeruManager 后端地址，默认 http://localhost:5555；
 *      远程部署填 http://<服务器IP>:<端口>，不要带 /api 后缀；
 *   3. 「访问令牌」填后端配置的收件箱令牌；后端未开启校验时留空；
 *   4. 「直接推送到软件」保持勾选：选中文字后点「发送到下载」直接进任务队列；
 *      关掉则只把文本复制到剪贴板，需要手动粘贴到下载面板。
 *
 * 三、用法
 *   1. 在任意网页选中含网盘链接的文字（可带 RJ 号 / 提取码 / 解压码）；
 *   2. 弹出识别框后点「发送到下载」：
 *        - 百度网盘  → 整理成「RJ 号 | 链接 | 提取码: xxx | 解压码: xxx」规范行；
 *        - Gofile / PikPak / Transfer.it → 只发链接，交给「HTTP 下载」解析；
 *   3. 在 KikoeruManager 的 HTTP 下载面板里预览、勾选文件并开始下载。
 *
 * 四、隐私
 *   脚本只把文本发给你自己填的后端地址，不经过任何第三方服务器；
 *   收件箱地址与令牌保存在 Tampermonkey 本地存储（GM_setValue），不会上传。
 * =========================================================================== */

(function () {
    'use strict';

    const customClass = {
        container: 'panai-container',
        popup: 'panai-popup',
    };

    let toast = Swal.mixin({
        toast: true,
        position: 'top',
        showConfirmButton: false,
        timer: 3500,
        timerProgressBar: false,
        didOpen: (toast) => {
            toast.addEventListener('mouseenter', Swal.stopTimer);
            toast.addEventListener('mouseleave', Swal.resumeTimer);
        }
    });

    let util = {
        clog(c) {
            console.group("%c %c [网盘智能识别助手]", `background:url(${GM_info.script.icon}) center center no-repeat;background-size:12px;padding:3px`, "");
            console.log(c);
            console.groupEnd();
        },

        parseQuery(name) {
            let reg = new RegExp(`(?<=(?:${name})\\=)(?:wss:[a-zA-Z0-9]+|[\\w-]+)`, "i")
            let pd = location.href.replace(/%3A/g, ":").match(reg);
            if (pd) {
                return pd[0];
            }
            return null;
        },

        getValue(name) {
            return GM_getValue(name);
        },

        setValue(name, value) {
            GM_setValue(name, value);
        },

        sleep(time) {
            return new Promise((resolve) => setTimeout(resolve, time));
        },

        escapeHtml(value) {
            return String(value === undefined || value === null ? '' : value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        },

        addStyle(id, tag, css) {
            tag = tag || 'style';
            let doc = document, styleDom = doc.getElementById(id);
            if (styleDom) return;
            let style = doc.createElement(tag);
            style.rel = 'stylesheet';
            style.id = id;
            tag === 'style' ? style.innerHTML = css : style.href = css;
            document.head.appendChild(style);
        },

        isHidden(el) {
            try {
                return el.offsetParent === null;
            } catch (e) {
                return false;
            }
        },

        isMobile: (() => !!navigator.userAgent.match(/(phone|pad|pod|iPhone|iPod|ios|iPad|Android|Mobile|BlackBerry|IEMobile|MQQBrowser|JUC|Fennec|wOSBrowser|BrowserNG|WebOS|Symbian|Windows Phone|HarmonyOS|MicroMessenger)/i))(),

        query(selector) {
            if (Array.isArray(selector)) {
                let obj = null;
                for (let i = 0; i < selector.length; i++) {
                    let o = document.querySelector(selector[i]);
                    if (o) {
                        obj = o;
                        break;
                    }
                }
                return obj;
            }
            return document.querySelector(selector);
        }
    };

    let opt = {
        'baidu': {
            reg: /((?:https?:\/\/)?(?:e?yun|pan)\.baidu\.com\/(doc\/|enterprise\/)?(?:s\/[\w~]*(((-)?\w*)*)?|share\/\S{4,}))/,
            host: /(pan|e?yun)\.baidu\.com/,
            input: ['#accessCode', '.share-access-code', '#wpdoc-share-page > .u-dialog__wrapper .u-input__inner'],
            button: ['#submitBtn', '.share-access .g-button', '#wpdoc-share-page > .u-dialog__wrapper .u-btn--primary'],
            name: '百度网盘',
            storage: 'hash'
        },
        'aliyun': {
            reg: /((?:https?:\/\/)?(?:(?:www\.)?(?:aliyundrive|alipan)\.com\/s|alywp\.net)\/[a-zA-Z\d]+)/,
            host: /www\.(aliyundrive|alipan)\.com|alywp\.net/,
            input: ['form .ant-input', 'form input[type="text"]', 'input[name="pwd"]'],
            button: ['form .button--fep7l', 'form button[type="submit"]'],
            name: '阿里云盘',
            storage: 'hash'
        },
        'weiyun': {
            reg: /((?:https?:\/\/)?share\.weiyun\.com\/[a-zA-Z\d]+)/,
            host: /share\.weiyun\.com/,
            input: ['.mod-card-s input[type=password]', 'input.pw-input'],
            button: ['.mod-card-s .btn-main', ".pw-btn-wrap button.btn"],
            name: '微云',
            storage: 'hash'
        },
        'lanzou': {
            reg: /((?:https?:\/\/)?(?:[a-zA-Z0-9\-.]+)?(?:lanzou[a-z]|lanzn)\.com\/[a-zA-Z\d_\-]+(?:\/[\w-]+)?)/,
            host: /(?:[a-zA-Z\d-.]+)?(?:lanzou[a-z]|lanzn)\.com/,
            input: ['#pwd'],
            button: ['.passwddiv-btn', '#sub'],
            name: '蓝奏云',
            storage: 'hash'
        },
        'tianyi': {
            reg: /((?:https?:\/\/)?cloud\.189\.cn\/(?:t\/|web\/share\?code=)?[a-zA-Z\d]+)/,
            host: /cloud\.189\.cn/,
            input: ['.access-code-item #code_txt', "input.access-code-input"],
            button: ['.access-code-item .visit', ".button"],
            name: '天翼云盘',
            storage: (() => util.isMobile === true ? 'local' : 'hash')(),
            storagePwdName: 'tmp_tianyi_pwd'
        },
        'caiyun': {
            reg: /((?:https?:\/\/)?caiyun\.139\.com\/(?:m\/i|w\/i\/|web\/|front\/#\/detail)\??(?:linkID=)?[a-zA-Z\d]+)/,
            host: /(?:cai)?yun\.139\.com/,
            input: ['.token-form input[type=text]'],
            button: ['.token-form .btn-token'],
            name: '移动云盘',
            storage: 'local',
            storagePwdName: 'tmp_caiyun_pwd'
        },
        'xunlei': {
            reg: /((?:https?:\/\/)?pan\.xunlei\.com\/s\/[\w-]{10,})/,
            host: /pan\.xunlei\.com/,
            input: ['.pass-input-wrap .td-input__inner'],
            button: ['.pass-input-wrap .td-button'],
            name: '迅雷云盘',
            storage: 'hash'
        },
        '123pan': {
            reg: /((?:https?:\/\/)?www\.123pan\.com\/s\/[\w-]{6,})/,
            host: /www\.123pan\.com/,
            input: ['.ca-fot input', ".appinput .appinput"],
            button: ['.ca-fot button', ".appinput button"],
            name: '123云盘',
            storage: 'hash'
        },
        '360': {
            reg: /((?:https?:\/\/)?(?:[a-zA-Z\d\-.]+)?(?:yunpan\.360\.cn|yunpan\.com)(\/lk)?\/surl_\w{6,})/,
            host: /[\w.]+?yunpan\.com/,
            input: ['.pwd-input'],
            button: ['.submit-btn'],
            name: '360云盘',
            storage: 'local',
            storagePwdName: 'tmp_360_pwd'
        },
        '115': {
            reg: /((?:https?:\/\/)?115\.com\/s\/[a-zA-Z\d]+)/,
            host: /115\.com/,
            input: ['.form-decode input'],
            button: ['.form-decode .submit a'],
            name: '115网盘',
            storage: 'hash'
        },
        'cowtransfer': {
            reg: /((?:https?:\/\/)?(?:[a-zA-Z\d-.]+)?cowtransfer\.com\/s\/[a-zA-Z\d]+)/,
            host: /(?:[a-zA-Z\d-.]+)?cowtransfer\.com/,
            input: ['.receive-code-input input'],
            button: ['.open-button'],
            name: '奶牛快传',
            storage: 'hash'
        },
        'ctfile': {
            reg: /((?:https?:\/\/)?(?:[a-zA-Z\d-.]+)?(?:ctfile|545c|u062|ghpym)\.com\/\w+\/[a-zA-Z\d-]+)/,
            host: /(?:[a-zA-Z\d-.]+)?(?:ctfile|545c|u062)\.com/,
            input: ['#passcode'],
            button: ['.card-body button'],
            name: '城通网盘',
            storage: 'hash'
        },
        'quark': {
            reg: /((?:https?:\/\/)?pan\.quark\.cn\/s\/[a-zA-Z\d-]+)/,
            host: /pan\.quark\.cn/,
            input: ['.ant-input'],
            button: ['.ant-btn-primary'],
            name: '夸克网盘',
            storage: 'local',
            storagePwdName: 'tmp_quark_pwd'
        },
        'vdisk': {
            reg: /(?:https?:\/\/)?vdisk.weibo.com\/lc\/\w+/,
            host: /vdisk\.weibo\.com/,
            input: ['#keypass', "#access_code"],
            button: ['.search_btn_wrap a', "#linkcommon_btn"],
            name: '微盘',
            storage: 'hash',
        },
        'wenshushu': {
            reg: /((?:https?:\/\/)?(?:www\.wenshushu|ws28)\.cn\/(?:k|box|f)\/\w+)/,
            host: /www\.wenshushu\.cn/,
            input: ['.pwd-inp .ivu-input'],
            button: ['.pwd-inp .ivu-btn'],
            name: '文叔叔网盘',
            storage: 'hash'
        },
        'uc': {
            reg: /(?:https?:\/\/)?drive\.uc\.cn\/s\/[a-zA-Z\d]+/,
            host: /drive\.uc\.cn/,
            input: ["input[class*='ShareReceivePC--input']", '.input-wrap input'],
            button: ["button[class*='ShareReceivePC--submit-btn'", '.input-wrap button'],
            name: 'UC云盘',
            storage: 'hash'
        },
        'jianguoyun': {
            reg: /((?:https?:\/\/)?www\.jianguoyun\.com\/p\/[\w-]+)/,
            host: /www\.jianguoyun\.com/,
            input: ['input[type=password]'],
            button: ['.ok-button', '.confirm-button'],
            name: '坚果云',
            storage: 'hash'
        },
        'wo': {
            reg: /(?:https?:\/\/)?pan\.wo\.cn\/s\/[\w_]+/,
            host: /(pan\.wo\.cn|panservice\.mail\.wo\.cn)/,
            input: ['input.el-input__inner', ".van-field__control"],
            button: ['.s-button', ".share-code button"],
            name: '联通云盘',
            storage: (() => util.isMobile === true ? 'local' : 'hash')(),
            storagePwdName: 'tmp_wo_pwd'
        },
        // Gofile / PikPak / Transfer.it：KikoeruManager 的「HTTP 下载」面板原生支持这三家，
        // 发送时走 provider=http（进外链面板，不进百度面板），分享密码拼成 ?pwd=。
        // 不设 storage：这些站点没有可靠的密码输入框选择器，避免 autoFillPassword
        // 用 undefined 选择器去 querySelector 抛错。
        'gofile': {
            reg: /((?:https?:\/\/)?(?:www\.)?gofile\.io\/(?:d|f|folder)\/[A-Za-z0-9_-]+(?:\?[A-Za-z0-9_=&%.,+-]*)?)/,
            host: /gofile\.io/,
            name: 'Gofile',
            httpPwd: true
        },
        'pikpak': {
            reg: /((?:https?:\/\/)?(?:www\.|drive\.)?mypikpak\.com\/s\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_.-]+)*(?:\?[A-Za-z0-9_=&%.,+-]*)?)/,
            host: /mypikpak\.com/,
            name: 'PikPak',
            httpPwd: true
        },
        // Transfer.it（WeTransfer 的端到端加密传输）：分享页形如 transfer.it/t/<id>。
        // 后端 http_download_service 用 transferit-py 走分享页解析 + 解密，因此插件侧
        // 只需把链接原样发给「HTTP 下载」，不要自己拼 pwd 或改写成直链。
        'transferit': {
            reg: /((?:https?:\/\/)?(?:www\.)?transfer\.it\/t\/[A-Za-z0-9_-]+(?:\?[A-Za-z0-9_=&%.,+-]*)?)/,
            host: /transfer\.it/,
            name: 'Transfer.it',
            httpPwd: true
        },
        'mega': {
            reg: /((?:https?:\/\/)?(?:mega\.nz|mega\.co\.nz)\/#F?![\w!-]+)/,
            host: /(?:mega\.nz|mega\.co\.nz)/,
            input: ['.dlkey-dialog input'],
            button: ['.dlkey-dialog .fm-dialog-new-folder-button'],
            name: 'Mega',
            storage: 'local'
        },
        '520vip': {
            reg: /((?:https?:\/\/)?www\.(?:520-vip|eos-53)\.com\/file-\d+\.html)/,
            host: /www\.520-vip\.com/,
            name: '520云盘',
        },
        '567pan': {
            reg: /((?:https?:\/\/)?www\.567(?:pan|yun|file|inc)\.(?:com|cn)\/file-\d+\.html)/,
            host: /www\.567inc\.cn/,
            name: '567盘',
            replaceHost: "www.567inc.com",
        },
        'ayunpan': {
            reg: /((?:https?:\/\/)?www\.ayunpan\.com\/file-\d+\.html)/,
            host: /www\.ayunpan\.com/,
            name: 'AYunPan',
        },
        'iycdn.com': {
            reg: /((?:https?:\/\/)?www\.iycdn\.com\/file-\d+\.html)/,
            host: /www\.iycdn\.com/,
            name: '爱优网盘',
        },
        'feimaoyun': {
            reg: /((?:https?:\/\/)?www\.feimaoyun\.com\/s\/[0-9a-zA-Z]+)/,
            host: /www\.feimaoyun\.com/,
            name: '飞猫盘',
        },
        'uyunp.com': {
            reg: /((?:https?:\/\/)?download\.uyunp\.com\/share\/s\/short\/\?surl=[0-9a-zA-Z]+)/,
            host: /download\.uyunp\.com/,
            name: '优云下载',
        },
        'dudujb': {
            reg: /(?:https?:\/\/)?www\.dudujb\.com\/file-\d+\.html/,
            host: /www\.dudujb\.com/,
            name: '贵族网盘',
        },
        'xunniu': {
            reg: /(?:https?:\/\/)?www\.xunniu(?:fxp|wp|fx)\.com\/file-\d+\.html/,
            host: /www\.xunniuwp\.com/,
            name: '迅牛网盘',
        },
        'xueqiupan': {
            reg: /(?:https?:\/\/)?www\.xueqiupan\.com\/file-\d+\.html/,
            host: /www\.xueqiupan\.com/,
            name: '雪球云盘',
        },
        '77file': {
            reg: /(?:https?:\/\/)?www\.77file\.com\/s\/[a-zA-Z\d]+/,
            host: /www\.77file\.com/,
            name: '77file',
        },
        'ownfile': {
            reg: /(?:https?:\/\/)?ownfile\.net\/files\/[a-zA-Z\d]+\.html/,
            host: /ownfile\.net/,
            name: 'OwnFile',
        },
        'feiyunfile': {
            reg: /(?:https?:\/\/)?www\.feiyunfile\.com\/file\/[\w=]+\.html/,
            host: /www\.feiyunfile\.com/,
            name: '飞云网盘',
        },
        'yifile': {
            reg: /(?:https?:\/\/)?www\.yifile\.com\/f\/\w+/,
            host: /www\.yifile\.com/,
            name: 'YiFile',
        },
        'dufile': {
            reg: /(?:https?:\/\/)?dufile\.com\/file\/\w+\.html/,
            host: /dufile\.com/,
            name: 'duFile',
        },
        'flowus': {
            reg: /((?:https?:\/\/)?flowus\.cn\/[\S ^\/]*\/?share\/[a-z\d]{8}-[a-z\d]{4}-[a-z\d]{4}-[a-z\d]{4}-[a-z\d]{12})/,
            host: /flowus\.cn/,
            name: 'FlowUs息流',
            storage: 'hash'
        },
        'chrome': {
            reg: /^https?:\/\/chrome.google.com\/webstore\/.+?\/([a-z]{32})(?=[\/#?]|$)/,
            host: /chrome\.google\.com/,
            replaceHost: "chrome.crxsoso.com",
            name: 'Chrome商店',
        },
        'edge': {
            reg: /^https?:\/\/microsoftedge.microsoft.com\/addons\/.+?\/([a-z]{32})(?=[\/#?]|$)/,
            host: /microsoftedge\.microsoft\.com/,
            replaceHost: "microsoftedge.crxsoso.com",
            name: 'Edge商店',
        },
        'firefox': {
            reg: /^https?:\/\/(reviewers\.)?(addons\.mozilla\.org|addons(?:-dev)?\.allizom\.org)\/.*?(?:addon|review)\/([^/<>"'?#]+)/,
            host: /addons\.mozilla\.org/,
            replaceHost: "addons.crxsoso.com",
            name: 'Firefox商店',
        },
        'microsoft': {
            reg: /^https?:\/\/(?:apps|www).microsoft\.com\/(?:store|p)\/.+?\/([a-zA-Z\d]{10,})(?=[\/#?]|$)/,
            host: /(apps|www)\.microsoft\.com/,
            replaceHost: "apps.crxsoso.com",
            name: 'Windows商店',
        }
    };

    let main = {
        lastText: "lorem&",

        // 预览框打开期间锁住识别（见 isIdentifyBlocked）
        dialogLock: false,

        // ==================================================================
        // KikoeruManager 下载收件箱
        // ------------------------------------------------------------------
        // 阶段一（当前）：解析 / 规范格式 / 预览编辑 / 校验 / 复制。
        // 阶段二（等后端 /api/download-inbox/push 发版）：设置里打开
        //   「启用真实推送」即可，requestPush 已按后端契约实现，无需改代码。
        // ==================================================================
        inboxDefaults: {
            url: 'http://localhost:5555'
        },

        // 解压码标签（后端 _TAG_NORMALIZE_RULES 的白名单，裸「密码」不算）
        // 裸「解压：xxx」这种省略「码」字的写法很常见（南+ 帖子），但必须紧跟
        // 冒号/等号才算——否则「解压后请使用密码」这类正文会被当成密码。
        // 注意：宽松标签只用于「识别」；写进规范行的必须是 STRICT 里带「码」的
        // 写法，后端 _EXTRACT_PASSWORD_LABEL_RE 不认裸「解压:」。
        EXTRACT_TAG: '(?:解压密码|解壓密碼|解压码|解壓碼|解压密碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼|解压暗号|解压(?=\\s*[:：=＝])|解壓(?=\\s*[:：=＝]))',
        EXTRACT_TAG_STRICT: '(?:解压密码|解壓密碼|解压码|解壓碼|解压密碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼|解压暗号)',
        // 解压码取值：遇到空白、分隔符、括号引号就截断。
        // 否则「解压：MHCA（请勿在线解压）」会把整句括号说明吃进密码。
        EXTRACT_VALUE_CLASS: '[^\\s,，。;；、|｜（）()【】「」『』《》<>"“”\'‘’]',
        EXTRACT_EXT_RE: /\.(?:zip|rar|7z|tar|gz|tgz|bz2|xz|zst|exe|txt|mp4|mkv|wav|flac|mp3|ape|lzh)$/i,
        // 「解压密码请见下方」这类正文，不能当成密码
        EXTRACT_STOPWORD_RE: /^(?:见|見|如下|下方|同上|无|無|没有|沒有|暂无|暫無|请|請|详见|詳見|参见|參見|不|未|略|自行|自己)/,

        //初始化配置数据
        initValue() {
            let value = [{
                name: 'setting_success_times',
                value: 0
            }, {
                name: 'setting_auto_click_btn',
                value: true
            }, {
                name: 'setting_active_in_front',
                value: true
            }, {
                name: 'setting_timer_open',
                value: false
            }, {
                name: 'setting_timer',
                value: 5000
            }, {
                name: 'setting_hotkeys',
                value: 'F1'
            }, {
                name: 'setting_inbox_show',
                value: true
            }, {
                name: 'setting_inbox_url',
                value: 'http://localhost:5555'
            }, {
                name: 'setting_inbox_token',
                value: ''
            }, {
                name: 'setting_inbox_channel',
                value: true
            }];

            value.forEach((v) => {
                if (util.getValue(v.name) === undefined) {
                    util.setValue(v.name, v.value);
                }
            });

            // 2.1.1：后端收件箱已发版，通道默认改为开启。
            // 2.1.0 会把 false 写进存储，这里做一次性升级（用户手动关掉后不再覆盖）。
            if (util.getValue('setting_inbox_channel_migrated') === undefined) {
                util.setValue('setting_inbox_channel', true);
                util.setValue('setting_inbox_channel_migrated', true);
            }
        },

        // 监听选择事件
        addPageListener() {
            document.addEventListener("mouseup", (event) => {
                if (this.isIdentifyBlocked(event)) return;
                this.smartIdentify(event);
            }, true);
            document.addEventListener("keydown", this.pressKey.bind(this), true);
        },

        // 弹窗内选字 / 弹窗打开时不得触发识别。
        // 成因：SweetAlert2 全局只允许一个弹窗，任何一次 Swal.fire 都会顶掉
        // 当前弹窗——在预览框里选中链接或文本松开鼠标，会走 smartIdentify
        // 再 fire 一个 toast，把正在填写的预览框直接关掉。
        isIdentifyBlocked(event) {
            if (this.dialogLock) return true;
            let target = event && event.target;
            if (target && target.closest) {
                // 弹窗（含 toast）内部的任何鼠标操作都不算页面选区
                if (target.closest('.swal2-container')) return true;
                if (target.closest('.panai-inbox')) return true;
                if (target.isContentEditable) return true;
                let tag = target.tagName;
                // 在输入框/文本域里拖选文字不应该触发识别
                if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
            }
            // 已有非 toast 弹窗处于打开状态
            let popup = document.querySelector('.swal2-container .swal2-popup');
            if (popup && !popup.classList.contains('swal2-toast')) return true;
            return false;
        },

        // ⚠️可能会增加时间⚠️ 如果有需要可以增加选项
        // 获取选择内容的HTML和文本(增加兼容) 或 DOM（节点遍历）
        getSelectionHTML(selection, isDOM = false) {
            const testDiv = document.createElement("div");
            if (!selection.isCollapsed) {
                // Range 转 DocumentFragment
                const docFragment = selection.getRangeAt(0).cloneContents();
                testDiv.appendChild(docFragment);
            }
            // 拼接选中文本，增加兼容
            return isDOM ? testDiv : selection.toString();
        },

        smartIdentify(event, str = '') {
            // 鼠标事件路径要守住弹窗；快捷键/剪贴板路径（event 为空）不拦
            if (event && this.isIdentifyBlocked(event)) return;
            let selection = window.getSelection();
            let text = str || this.getSelectionHTML(selection);
            if (text !== this.lastText && text !== '') { //选择相同文字或空不识别
                let start = performance.now();
                this.lastText = text;
                //util.clog(`当前选中文字：${text}`);
                let linkObj = this.parseLink(text);
                let link = linkObj.link;
                let name = linkObj.name;
                if (!link) {
                    linkObj = this.parseParentLink(selection);
                    link = linkObj.link;
                    name = linkObj.name;
                }
                if (link) {
                    if (!/https?:\/\//.test(link)) {
                        link = 'https://' + link;
                    }
                    // 只有百度网盘走「规范格式」；Gofile / PikPak 走 HTTP 下载；
                    // 其他网盘只发送链接本身
                    let isBaidu = name === opt.baidu.name;
                    let pwdShare = this.isHttpPwdShare(name);
                    // 提取码与解压码分开识别：「解压密码」绝不能被当成提取码拼进 ?pwd=。
                    // Gofile / PikPak 的分享链接本身不需要这两个码，帖子里的
                    // 「提取：」「解压：」是给压缩包或别的网盘写的，直接跳过不解析。
                    let pwd = pwdShare ? '' : this.parseTransferCode(text);
                    let extractInfo = pwdShare ? {value: '', source: ''} : this.resolveExtractCode([text]);
                    let extractCode = extractInfo.value;

                    let end = performance.now();
                    let time = (end - start).toFixed(3);
                    util.clog(`文本识别结果：${name} 链接：${link} 提取码：${pwd || '-'} 解压码：${extractCode || '-'} 耗时：${time}毫秒`);

                    // 分享密码默认留空，真遇到加密分享再在弹窗里手填。
                    let sharePwd = pwdShare ? '' : pwd;
                    let openPwd = sharePwd;

                    let codeParts = [];
                    if (!pwdShare) {
                        if (pwd) codeParts.push('提取码：' + pwd);
                        if (extractCode) codeParts.push(extractInfo.source === 'bare' ? '密码：' + extractCode + '（按解压码）' : '解压码：' + extractCode);
                    }
                    let codeText = pwdShare
                        ? '无需提取码 / 解压码，是否打开？'
                        : (codeParts.length ? codeParts.join('　') : '是否打开？');

                    let inboxShow = !!util.getValue('setting_inbox_show');
                    let inboxHint = '';
                    if (inboxShow && isBaidu) {
                        let titleRj = this.pickMaxRj(document.title);
                        inboxHint = `<div style="font-size:0.72em;color:#999;margin-top:4px;line-height:16px;">${titleRj ? 'RJ：' + util.escapeHtml(titleRj) : '标签页标题里没有 RJ 号'}</div>`;
                    }

                    let option = {
                        toast: true,
                        showCancelButton: true,
                        position: 'top',
                        title: `发现<span style="color: #2778c4;margin: 0 5px;">${name}</span>链接`,
                        html: `<span style="font-size: 0.8em;">${util.escapeHtml(codeText)}</span>${inboxHint}`,
                        confirmButtonText: '打开',
                        cancelButtonText: '关闭',
                        customClass
                    };
                    if (inboxShow) {
                        option.showDenyButton = true;
                        option.denyButtonText = isBaidu ? '发送' : (pwdShare ? '发送到下载' : '发送链接');
                        option.denyButtonColor = '#2e7d32';
                        option.width = 480;
                    }
                    if (util.getValue('setting_timer_open')) {
                        option.timer = util.getValue('setting_timer');
                        option.timerProgressBar = true;
                    }
                    util.setValue('setting_success_times', util.getValue('setting_success_times') + 1);

                    Swal.fire(option).then((res) => {
                        this.lastText = 'lorem&';
                        selection.empty();
                        if (res.isDenied) {
                            this.openInboxDialog({
                                isBaidu: isBaidu,
                                pwdShare: pwdShare,
                                name: name,
                                url: this.resolveShareUrl(linkObj, link),
                                text: text,
                                pwd: openPwd,
                                extract: extractCode,
                                extractSource: extractInfo.source
                            });
                            return;
                        }
                        if (res.isConfirmed || res.dismiss === 'timer') {
                            if (linkObj.storage == "local") {
                                util.setValue(linkObj.storagePwdName, openPwd);
                            }
                            let active = util.getValue('setting_active_in_front');
                            if (openPwd) {
                                let extra = `${link}?pwd=${openPwd}#${openPwd}`;
                                if (~link.indexOf('?')) {
                                    extra = `${link}&pwd=${openPwd}#${openPwd}`;
                                }
                                GM_openInTab(extra, {active});
                            } else {
                                GM_openInTab(`${link}`, {active});
                            }
                        }
                    });
                }
            }
        },

        pressKey(event) {
            // 输入框/文本框里回车不应触发弹窗确认按钮（新增的预览框是 textarea）
            let tag = event.target && event.target.tagName;
            if (event.key === 'Enter' && (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT')) {
                return;
            }
            if (event.key === 'Enter') {
                let confirmBtn = document.querySelector('.panai-container .swal2-confirm');
                confirmBtn && confirmBtn.click();
            }
            if (event.key === 'Escape') {
                let cancelBtn = document.querySelector('.panai-container .swal2-cancel');
                cancelBtn && cancelBtn.click();
            }
        },

        addHotKey() {
            //获取设置中的快捷键
            let hotkey = util.getValue('setting_hotkeys');
            hotkeys(hotkey, (event, handler) => {
                event.preventDefault();
                this.showIdentifyBox();
            });
        },

        //正则解析网盘链接
        parseLink(text = '') {
            let obj = {name: '', link: '', storage: '', storagePwdName: ''};
            if (text) {
                try {
                    text = decodeURIComponent(text);
                } catch {
                }
                text = text.replace(/[点點]/g, '.');
                text = text.replace(/[\u4e00-\u9fa5()（）,\u200B，\uD83C-\uDBFF\uDC00-\uDFFF]/g, '');
                text = text.replace(/lanzous/g, 'lanzouw'); //修正lanzous打不开的问题
                for (let name in opt) {
                    let val = opt[name];
                    if (val.reg.test(text)) {
                        let matches = text.match(val.reg);
                        obj.name = val.name;
                        obj.link = matches[0];
                        obj.storage = val.storage;
                        obj.storagePwdName = val.storagePwdName || null;
                        if (val.replaceHost) {
                            obj.link = obj.link.replace(val.host, val.replaceHost);
                        }
                        return obj;
                    }
                }
            }
            return obj;
        },

        //正则解析超链接类型网盘链接
        parseParentLink(selection) {
            const dom = this.getSelectionHTML(selection, true).querySelector('*[href]');
            return this.parseLink(dom ? dom.href : "");
        },

        // 提取码专用扫描：把「标签 + 值」一起匹配，命中解压类标签时整段跳过。
        // 旧实现用 [密] 单字做前缀，导致「解压密码：xxx」被当成提取码。
        // 「提取：xxx」（省略「码」字）同样常见，但要求紧跟冒号/等号，
        // 这样「请提取 1234 号文件」这类正文不会被误判。
        PWD_SCAN_RE: /(解压密码|解壓密碼|解压码|解壓碼|解压密碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼|解压暗号|解壓暗號|提取码|提取碼|提取口令|提取密码|提取密碼|提取(?=\s*[:：=＝])|访问码|訪問碼|访问密码|訪問密碼|key|password|pwd)\s*[:：=＝]?\s*([A-Za-z0-9]{3,12})/gi,
        PWD_EXTRACT_TAG_RE: /^(?:解压|解壓|压缩|壓縮)/,

        //正则解析提取码（只认提取码类标签，解压码一律跳过）
        parseTransferCode(text) {
            let raw = String(text || '').replace(/\u200B/g, '').replace('%3A', ':')
                // 全角字母/数字（中文输入法常见）先折半角，否则「提取：ｇ9be」匹配不到
                .replace(/[\uFF01-\uFF5E]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0));
            if (!raw) return '';
            // 百度口令形态
            let wss = raw.match(/wss:[a-zA-Z0-9]+/i);
            if (wss) return wss[0];

            let cleaned = raw.replace(/(?:本帖)?隐藏的?内容[：:]?/, "");
            this.PWD_SCAN_RE.lastIndex = 0;
            let match;
            while ((match = this.PWD_SCAN_RE.exec(cleaned)) !== null) {
                let tag = match[1].replace(/\s/g, '');
                if (this.PWD_EXTRACT_TAG_RE.test(tag)) continue; // 解压码不是提取码
                let value = match[2];
                // 排除明显不是密码的裸数字/占位词
                if (/^(?:无|暂无|没有|null|none)$/i.test(value)) continue;
                return value;
            }

            // 文本里自带的链接参数兜底
            let urlPwd = cleaned.match(/[?&](?:pwd|p)=([A-Za-z0-9]{3,12})(?![A-Za-z0-9])/i);
            if (urlPwd) return urlPwd[1];
            return '';
        },

        // 裸「密码」：ASMR 帖子里这类几乎都是压缩包密码（解压码），不能当提取码。
        // 排除「提取密码 / 访问密码」这类明确指提取码的写法。
        // 允许标签与值之间隔一个换行（帖子常把「密码：」写在行尾），
        // 但取值必须含 ASCII 字母数字，纯中文正文（「密码：下载后解压」）仍会被拦掉。
        PWD_BARE_RE: /(?<![提取访訪问問])(?:密码|密碼)[ \t]*(?:[:：=＝][ \t]*(?:\r?\n[ \t]*)?|[ \t]+)([^\s,，。;；、|｜（）()【】「」『』《》<>"“”'‘’]+)/i,

        findBarePassword(text) {
            if (!text) return '';
            let cleaned = String(text).replace(/https?:\/\/[^\s<>"'|｜]+/gi, ' ');
            let match = cleaned.match(this.PWD_BARE_RE);
            if (!match) return '';
            // 裸「密码」歧义最大：要求值里至少有一个 ASCII 字母/数字，
            // 否则「密码 在这里」这类正文会被误当密码。纯中文密码请用「解压码：」标签。
            if (!/[A-Za-z0-9]/.test(match[1])) return '';
            let raw = match[1].replace(/^(?:是|为|為)/, '');
            if (this.EXTRACT_STOPWORD_RE.test(raw)) return '';
            return this.normalizeExtractCode(raw);
        },

        // 解压码解析：先找带明确标签的（跨来源优先级最高），再退到裸「密码」
        resolveExtractCode(sources) {
            let list = (Array.isArray(sources) ? sources : [sources]).filter((item) => !!item);
            for (let i = 0; i < list.length; i++) {
                let tagged = this.findExtractCode(list[i]);
                if (tagged) return {value: tagged, source: 'tag'};
            }
            for (let i = 0; i < list.length; i++) {
                let bare = this.findBarePassword(list[i]);
                if (bare) return {value: bare, source: 'bare'};
            }
            return {value: '', source: ''};
        },

        //根据域名检测网盘类型
        panDetect() {
            let hostname = location.hostname;
            for (let name in opt) {
                let val = opt[name];
                if (val.host.test(hostname)) {
                    return name;
                }
            }
            return '';
        },

        //自动填写密码
        autoFillPassword() {
            let query = util.parseQuery('pwd|p');
            let hash = location.hash.slice(1).replace(/\W/g, "") //hash中可能存在密码，需要过滤掉非密码字符
            let pwd = query || hash;
            let panType = this.panDetect();
            for (let name in opt) {
                let val = opt[name];
                if (panType === name) {
                    if (val.storage === 'local') {
                        //当前local存储的密码不一定是当前链接的密码，用户可能通过url直接访问或者恢复页面，这样取出来的密码可能是其他链接的
                        //如果能从url中获取到密码，则应该优先使用url中获取的密码
                        //util.getValue查询不到key时，默认返回undefined，已经形成逻辑短路，此处赋空值无效也无需赋空值.详见https://github.com/syhyz1990/panAI/commit/efb6ff0c77972920b26617bb836a2e19dd14a749
                        pwd = pwd || util.getValue(val.storagePwdName);
                        pwd && this.doFillAction(val.input, val.button, pwd);
                    }
                    if (val.storage === 'hash') {
                        if (!/^(?:wss:[a-zA-Z\d]+|[a-zA-Z0-9]{3,8})$/.test(pwd)) { //过滤掉不正常的Hash
                            return;
                        }
                        pwd && this.doFillAction(val.input, val.button, pwd);
                    }
                }
            }
        },

        doFillAction(inputSelector, buttonSelector, pwd) {
            let maxTime = 10;
            let ins = setInterval(async () => {
                maxTime--;
                let input = util.query(inputSelector);
                let button = util.query(buttonSelector);
                if (input && !util.isHidden(input)) {
                    clearInterval(ins);
                    Swal.fire({
                        toast: true,
                        position: 'top',
                        showCancelButton: false,
                        showConfirmButton: false,
                        title: 'AI已识别到密码！正自动帮您填写',
                        icon: 'success',
                        timer: 2000,
                        customClass
                    });

                    let lastValue = input.value;
                    input.value = pwd;
                    //Vue & React 触发 input 事件
                    let event = new Event('input', {bubbles: true});
                    let tracker = input._valueTracker;
                    if (tracker) {
                        tracker.setValue(lastValue);
                    }
                    input.dispatchEvent(event);

                    if (util.getValue('setting_auto_click_btn')) {
                        await util.sleep(1000); //1秒后点击按钮
                        button.click();
                    }
                } else {
                    maxTime === 0 && clearInterval(ins);
                }
            }, 800);
        },

        //重置识别次数
        clearIdentifyTimes() {
            let res = Swal.fire({
                showCancelButton: true,
                title: '确定要重置识别次数吗？',
                icon: 'warning',
                confirmButtonText: '确定',
                cancelButtonText: '取消',
                customClass
            }).then(res => {
                this.lastText = 'lorem&';
                if (res.isConfirmed) {
                    util.setValue('setting_success_times', 0);
                    history.go(0);
                }
            });
        },

        //识别输入框中的内容
        showIdentifyBox() {
            Swal.fire({
                title: '识别剪切板中文字',
                input: 'textarea',
                inputPlaceholder: '若选方式一，请按 Ctrl+V 粘贴要识别的文字',
                html: `<div style="font-size: 12px;color: #999;margin-bottom: 8px;text-align: center;">提示：在任意网页按下 <span style="font-weight: 700;">${util.getValue("setting_hotkeys")}</span> 键可快速打开本窗口。</div><div style="font-size: 14px;line-height: 22px;padding: 10px 0 5px;text-align: left;"><div style="font-size: 16px;margin-bottom: 8px;font-weight: 700;">支持以下两种方式：</div><div><b>方式一：</b>直接粘贴文字到输入框，点击“识别方框内容”按钮。</div><div><b>方式二：</b>点击“读取剪切板”按钮。<span style="color: #d14529;font-size: 12px;">会弹出“授予网站读取剪切板”权限，同意后会自动识别剪切板中的文字。</span></div></div>`,
                showCloseButton: false,
                showDenyButton: true,
                confirmButtonText: '识别方框内容',
                denyButtonText: '读取剪切板',
                customClass
            }).then(res => {
                if (res.isConfirmed) {
                    this.smartIdentify(null, res.value);
                }
                if (res.isDenied) {
                    navigator.clipboard.readText().then(text => {
                        this.smartIdentify(null, text);
                    }).catch(() => {
                        toast.fire({title: '读取剪切板失败，请先授权或手动粘贴后识别！', icon: 'error'});
                    });
                }
            });
        },

        //显示设置
        showSettingBox() {
            let html = `<div style="font-size: 1em;">
                              <label class="panai-setting-label">填写密码后自动提交<input type="checkbox" id="S-Auto" ${util.getValue('setting_auto_click_btn') ? 'checked' : ''} class="panai-setting-checkbox"></label>
                              <label class="panai-setting-label">前台打开网盘标签页<input type="checkbox" id="S-Active" ${util.getValue('setting_active_in_front') ? 'checked' : ''}
                              class="panai-setting-checkbox"></label>
                              <label class="panai-setting-label">倒计时结束自动打开<input type="checkbox" id="S-Timer-Open" ${util.getValue('setting_timer_open') ? 'checked' : ''} class="panai-setting-checkbox"></label>
                              <label class="panai-setting-label" id="Panai-Range-Wrapper" style="${util.getValue('setting_timer_open') ? '' : 'display: none'}"><span>倒计时 <span id="Timer-Value">（${util.getValue('setting_timer') / 1000}秒）</span></span><input type="range" id="S-Timer" min="0" max="10000" step="500" value="${util.getValue('setting_timer')}" style="width: 200px;"></label>
                              <label class="panai-setting-label">快捷键设置<input type="text" id="S-hotkeys" value="${util.getValue('setting_hotkeys')}" style="width: 100px;"></label>
                              <div class="panai-setting-divider">KikoeruManager 收件箱</div>
                              <label class="panai-setting-label">识别后显示发送按钮<input type="checkbox" id="S-Inbox-Show" ${util.getValue('setting_inbox_show') ? 'checked' : ''} class="panai-setting-checkbox"></label>
                              <label class="panai-setting-label">收件箱地址<input type="text" id="S-Inbox-Url" value="${util.escapeHtml(util.getValue('setting_inbox_url'))}" placeholder="http://localhost:5555" style="width: 220px;"></label>
                              <label class="panai-setting-label">访问令牌<input type="text" id="S-Inbox-Token" value="${util.escapeHtml(util.getValue('setting_inbox_token') || '')}" placeholder="留空表示不校验" style="width: 220px;"></label>
                              <label class="panai-setting-label">直接推送到软件<input type="checkbox" id="S-Inbox-Channel" ${util.getValue('setting_inbox_channel') ? 'checked' : ''} class="panai-setting-checkbox"></label>
                              <div class="panai-setting-hint">后端收件箱已发版，保持开启即可直接推送；关掉后只把规范文本复制到剪贴板。</div>
                            </div>`;
            Swal.fire({
                title: '识别助手配置',
                html,
                icon: 'info',
                showCloseButton: true,
                confirmButtonText: '保存',
                footer: '<div style="text-align: center;font-size: 1em;">点击查看 <a href="https://www.youxiaohou.com/tool/install-panai.html" target="_blank">使用说明</a>，助手免费开源，Powered by <a href="https://www.youxiaohou.com">油小猴</a></div>',
                customClass
            }).then((res) => {
                res.isConfirmed && history.go(0);
            });

            document.getElementById('S-Auto').addEventListener('change', (e) => {
                util.setValue('setting_auto_click_btn', e.target.checked);
            });
            document.getElementById('S-Active').addEventListener('change', (e) => {
                util.setValue('setting_active_in_front', e.target.checked);
            });
            document.getElementById('S-Timer-Open').addEventListener('change', (e) => {
                let rangeWrapper = document.getElementById('Panai-Range-Wrapper');
                e.target.checked ? rangeWrapper.style.display = 'flex' : rangeWrapper.style.display = 'none';
                util.setValue('setting_timer_open', e.target.checked);
            });
            document.getElementById('S-Timer').addEventListener('change', (e) => {
                util.setValue('setting_timer', e.target.value);
                document.getElementById('Timer-Value').innerText = `（${e.target.value / 1000}秒）`;
            });
            document.getElementById('S-hotkeys').addEventListener('change', (e) => {
                util.setValue('setting_hotkeys', e.target.value);
            });
            document.getElementById('S-Inbox-Show').addEventListener('change', (e) => {
                util.setValue('setting_inbox_show', e.target.checked);
            });
            document.getElementById('S-Inbox-Url').addEventListener('input', (e) => {
                util.setValue('setting_inbox_url', e.target.value.trim());
            });
            document.getElementById('S-Inbox-Token').addEventListener('input', (e) => {
                util.setValue('setting_inbox_token', e.target.value.trim());
            });
            document.getElementById('S-Inbox-Channel').addEventListener('change', (e) => {
                util.setValue('setting_inbox_channel', e.target.checked);
            });
        },

        // ================== 收件箱：字段解析 ==================

        // 从文本里收集所有 RJ 号（用于"多个取数值最大"）
        findRjCandidates(text) {
            let list = [];
            if (!text) return list;
            let reg = /(?<![A-Za-z0-9])RJ[\s_\-:：]*(\d{5,8})(?!\d)/gi;
            let match;
            while ((match = reg.exec(String(text))) !== null) {
                let digits = match[1];
                list.push({code: 'RJ' + digits, value: parseInt(digits, 10)});
            }
            return list;
        },

        // 取数值最大的 RJ 号；没有则返回 ''
        pickMaxRj(text) {
            let best = null;
            this.findRjCandidates(text).forEach((item) => {
                if (!best || item.value > best.value) best = item;
            });
            return best ? best.code : '';
        },

        // 把用户输入/页面内容里的 RJ 规范成 RJ01234567
        normalizeRj(raw) {
            let value = String(raw === undefined || raw === null ? '' : raw)
                .replace(/[\s\u3000\[\]【】()（）]/g, '')
                .toUpperCase();
            if (/^\d{5,8}$/.test(value)) value = 'RJ' + value;
            return value;
        },

        // 提取码：只允许 4~12 位字母数字
        normalizeTransferCode(raw) {
            let value = this.cleanTransferInput(raw);
            return /^[A-Za-z0-9]{4,12}$/.test(value) ? value : '';
        },

        // 提取码清洗（不淘汰，交给校验提示）
        // 中文输入法下「ｇ9be」这种全角字母很常见，先折成半角再过滤。
        cleanTransferInput(raw) {
            return String(raw === undefined || raw === null ? '' : raw)
                .replace(/[\uFF01-\uFF5E]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0))
                .replace(/[\s\u3000]/g, '')
                .replace(/[^A-Za-z0-9]/g, '');
        },

        // 解压码清洗：只「去噪」，不做字符集淘汰——用户手输的值被静默清空
        // 是最糟的体验。去噪规则对齐后端 preview_name_hints._clean_password：
        // 去首尾空白、控制字符、误带的压缩包后缀、句末标点与括号。
        cleanExtractInput(raw) {
            let value = String(raw === undefined || raw === null ? '' : raw);
            // 换行/制表符会把一行规范格式拆坏
            value = value.replace(/[\r\n\t]+/g, ' ').trim();
            value = value.replace(/[\u200B-\u200D\uFEFF]/g, '');
            // 只折全角字母数字，中文全角标点（括号、逗号）保留原样
            value = value.replace(/[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/g, (ch) => String.fromCharCode(ch.charCodeAt(0) - 0xFEE0));
            value = value.replace(this.EXTRACT_EXT_RE, '');
            value = value.replace(/[)\]）〕}>》」』】'"`”’。，；、,.\s]+$/g, '');
            return value;
        },

        // 解压码：允许 @ 中文等任意符号，只排除会把规范行拆段的字符
        // （空白、竖线、逗号、顿号、分号、句号）——后端取值为 [^\s,，。;；、|｜]+
        normalizeExtractCode(raw) {
            let value = this.cleanExtractInput(raw);
            if (!value || value.length > 64) return '';
            if (/[\s,，。;；、|｜]/.test(value)) return '';
            // 「解压码****」这类页面掩码不是真密码，别当密码发出去
            if (!/[\p{L}\p{N}]/u.test(value)) return '';
            return value;
        },

        // 从页面/选中文字里找解压码（含「解压码abc123.zip」这种文件名写法）
        // 标签与值之间不止「：」一种连法，帖子里的实际写法还有：
        //   解压密码是 MHCA / 解压：\nMHCA / 解压：MHCA（请勿在线解压）
        // 所以这里把「冒号/等号」「是/为」「一个换行」都当成连接符，
        // 取值遇到括号、引号即截断。跨行只在值含 ASCII 字母数字时才认，
        // 避免把「解压：」下一行的正文当成密码。
        findExtractCode(text) {
            if (!text) return '';
            let cleaned = String(text).replace(/https?:\/\/[^\s<>"'|｜]+/gi, ' ');
            let reg = new RegExp(
                this.EXTRACT_TAG +
                '[ \\t]*(?:[:：=＝][ \\t]*)?(?:(?:是|为|為)[ \\t]*[:：=＝]?[ \\t]*)?(?:\\r?\\n[ \\t]*)?' +
                '(' + this.EXTRACT_VALUE_CLASS + '+)',
                'i'
            );
            let match = cleaned.match(reg);
            if (!match) return '';
            let raw = match[1];
            if (this.EXTRACT_STOPWORD_RE.test(raw)) return '';
            if (/\r?\n/.test(match[0]) && !/[A-Za-z0-9]/.test(raw)) return '';
            return this.normalizeExtractCode(raw);
        },

        getPageText() {
            try {
                let text = document.body ? (document.body.innerText || '') : '';
                return String(text).slice(0, 300000);
            } catch (e) {
                return '';
            }
        },

        // 拼一行规范格式：RJ 号 → 链接 → 提取码 → 解压码，空段整段省略
        composeLine(fields) {
            let parts = [];
            if (fields.rj) parts.push(fields.rj);
            if (fields.url) parts.push(String(fields.url).trim());
            if (!fields.urlHasPwd && fields.code) parts.push('提取码: ' + fields.code);
            if (fields.extract) parts.push('解压码: ' + fields.extract);
            return parts.join(' | ');
        },

        // 非百度分享（Gofile / PikPak）发送到「HTTP 下载」：只发链接本身，
        // 分享密码拼成 ?pwd=（后端 gofile/pikpak 解析器会读 password/pwd/code）。
        // 链接里已经带密码参数时不重复拼，避免出现两个 pwd。
        composeHttpLine(url, code) {
            let value = String(url || '').trim();
            let pwd = String(code || '').trim();
            if (!value || !pwd) return value;
            if (/[?&](?:pwd|password|passcode|pass_code|code)=/i.test(value)) return value;
            return value + (~value.indexOf('?') ? '&' : '?') + 'pwd=' + encodeURIComponent(pwd);
        },

        // 该网盘是否属于「发送到 HTTP 下载」并支持把分享密码拼成 ?pwd=
        isHttpPwdShare(name) {
            for (let key in opt) {
                if (opt[key].name === name) return !!opt[key].httpPwd;
            }
            return false;
        },

        // 校验百度网盘规范行
        validateLine(text) {
            let lines = String(text || '').split(/\r?\n/).map((item) => item.trim()).filter((item) => item);
            if (!lines.length) return {ok: false, message: '内容为空，无法发送'};
            for (let index = 0; index < lines.length; index++) {
                let line = lines[index];
                let prefix = `第 ${index + 1} 行`;
                if (/｜/.test(line)) {
                    return {ok: false, message: `${prefix}用了全角竖线「｜」，分隔符必须是半角 « | »`};
                }
                if (!/^RJ\d{5,8}(?:\s\|\s|$)/i.test(line)) {
                    return {ok: false, message: `${prefix}不是以 RJ 号开头（正确写法 RJ01234567），KikoeruManager 不会自动改名`};
                }
                if (!/\|\s*https?:\/\/\S+/i.test(line)) {
                    return {ok: false, message: `${prefix}缺少有效的分享链接`};
                }
                // 逐段校验：解压码允许 @ / 中文等任意符号，但如果值里混进半角竖线，
                // 整行会被拆成多段、密码被截断，这里必须报错而不是放过。
                let segments = line.split('|').map((item) => item.trim()).filter((item) => item);
                for (let s = 0; s < segments.length; s++) {
                    let seg = segments[s];
                    if (/^RJ\d{5,8}$/i.test(seg)) continue;
                    if (/^https?:\/\/\S+$/i.test(seg)) continue;
                    let transfer = /^(?:提取码|提取碼|提取口令|访问码|訪問碼)\s*[:：]\s*(.*)$/.exec(seg);
                    if (transfer) {
                        if (!/^[A-Za-z0-9]{4,12}$/.test(transfer[1].trim())) {
                            return {ok: false, message: `${prefix}的提取码必须是 4~12 位字母数字`};
                        }
                        continue;
                    }
                    let extract = new RegExp('^(?:' + this.EXTRACT_TAG_STRICT + ')\\s*[:：]\\s*(.*)$', 'i').exec(seg);
                    if (extract) {
                        let value = extract[1].trim();
                        if (!value) {
                            return {ok: false, message: `${prefix}的「解压码:」后面没有值`};
                        }
                        if (/[\s,，。;；、]/.test(value)) {
                            return {ok: false, message: `${prefix}的解压码不能包含空格、逗号、顿号或分号（会把规范行拆坏）`};
                        }
                        if (value.length > 64) {
                            return {ok: false, message: `${prefix}的解压码过长（最多 64 个字符）`};
                        }
                        continue;
                    }
                    if (/^(?:密码|密碼)\s*[:：]/.test(seg)) {
                        return {ok: false, message: `${prefix}的密码段必须写成「解压码: xxx」，裸「密码:」会被丢弃`};
                    }
                    if (/^(?:提取|解压|解壓)\s*[:：]/.test(seg)) {
                        let label = /^(?:提取|解压|解壓)/.exec(seg)[0];
                        let fix = label === '提取' ? '提取码' : '解压码';
                        return {ok: false, message: `${prefix}的「${label}:」后端不认，要写成「${fix}: xxx」`};
                    }
                    return {ok: false, message: `${prefix}有一段「${seg}」无法识别（密码里不能出现半角竖线 |，它会把密码截断）`};
                }
            }
            return {ok: true, lines};
        },

        // 校验非百度网盘：只要求是 http(s) 链接
        validateLinkOnly(text) {
            let lines = String(text || '').split(/\r?\n/).map((item) => item.trim()).filter((item) => item);
            if (!lines.length) return {ok: false, message: '内容为空，无法发送'};
            for (let index = 0; index < lines.length; index++) {
                if (!/^https?:\/\/\S+$/i.test(lines[index])) {
                    return {ok: false, message: `第 ${index + 1} 行不是有效的 http(s) 链接`};
                }
            }
            return {ok: true, lines};
        },

        // 分享链接：优先用当前页面地址（保留 pwd 等原始查询参数）
        resolveShareUrl(linkObj, fallbackLink) {
            try {
                let panType = this.panDetect();
                if (panType && linkObj && linkObj.name && opt[panType] && opt[panType].name === linkObj.name) {
                    let val = opt[panType];
                    let href = location.origin + location.pathname + location.search;
                    if (!val.reg || val.reg.test(href)) return href;
                }
            } catch (e) {
            }
            return fallbackLink || '';
        },

        // ================== 收件箱：地址 / 通道 ==================

        getBaseUrl() {
            let value = String(util.getValue('setting_inbox_url') || this.inboxDefaults.url).trim();
            if (!value) value = this.inboxDefaults.url;
            if (!/^https?:\/\//i.test(value)) value = 'http://' + value;
            // 允许用户直接粘贴「…/api/download-inbox/push」
            value = value.replace(/\/+$/, '').replace(/\/api(\/download-inbox\/push)?$/i, '');
            return value;
        },

        getToken() {
            return String(util.getValue('setting_inbox_token') || '').trim();
        },

        isChannelOn() {
            return !!util.getValue('setting_inbox_channel');
        },

        // 后端 SUPPORTED_PROVIDERS = ("baidu", "http")。
        // Gofile / PikPak 必须走 http（HTTP 下载面板）：传 gofile / pikpak
        // 会被 normalize_provider 当成未知值，一律归到百度面板。
        inboxProvider(isBaidu) {
            return isBaidu ? 'baidu' : 'http';
        },

        // ================== 收件箱：预览 / 编辑 / 发送 ==================

        openInboxDialog(ctx) {
            let self = this;
            let baseUrl = this.getBaseUrl();
            let channelOn = this.isChannelOn();
            let isBaidu = !!ctx.isBaidu;
            let pwdShare = !!ctx.pwdShare;

            // RJ 号：标签页标题优先，多个取数值最大
            let rjSource = '标签页标题';
            let rj = this.pickMaxRj(document.title);
            if (!rj && ctx.text) {
                let alt = this.pickMaxRj(ctx.text);
                if (alt) {
                    rj = alt;
                    rjSource = '选中文字（标题无 RJ）';
                }
            }
            if (!rj) rjSource = '未识别，请手动填写';

            let url = String(ctx.url || '').trim();
            // 解压码：只有百度规范格式需要；Gofile / PikPak 一律留空，
            // 连整页文本都不扫，避免把压缩包密码误当分享密码。
            let extractInfo = { value: '', source: '' };
            if (!pwdShare) {
                // 优先用识别阶段的结果；没有就依次从「选中文字 → 整页文本」找
                // （带明确标签的优先，其次才是裸「密码」）
                extractInfo = {
                    value: this.cleanExtractInput(ctx.extract || ''),
                    source: ctx.extractSource || ''
                };
                if (!extractInfo.value) {
                    extractInfo = this.resolveExtractCode([ctx.text || '', this.getPageText()]);
                }
            }
            let fields = {
                rj: this.normalizeRj(rj),
                url: url,
                urlHasPwd: /[?&](?:pwd|password|passcode|pass_code|code)=/i.test(url),
                // 两家默认不带任何码；用户手填的分享密码才进 code
                code: pwdShare ? '' : this.cleanTransferInput(ctx.pwd || ''),
                extract: extractInfo.value
            };

            let title = isBaidu ? '发送到 KikoeruManager' : (pwdShare ? `发送 ${ctx.name || '链接'} 到 HTTP 下载` : '发送链接到 KikoeruManager');
            let preview = isBaidu
                ? this.composeLine(fields)
                : (pwdShare ? this.composeHttpLine(url, fields.code) : url);

            let bodyHtml;
            if (isBaidu) {
                bodyHtml = `
                    <div class="panai-inbox">
                        <div class="panai-inbox-meta">目标：${util.escapeHtml(baseUrl)}</div>
                        <div class="panai-inbox-fields">
                            <label class="panai-inbox-field">
                                <span>RJ 号<em class="panai-inbox-src">${util.escapeHtml(rjSource)}</em></span>
                                <input type="text" id="Panai-Rj" value="${util.escapeHtml(fields.rj)}" placeholder="RJ01234567" autocomplete="off" spellcheck="false">
                            </label>
                            <label class="panai-inbox-field">
                                <span>提取码</span>
                                <input type="text" id="Panai-Code" value="${util.escapeHtml(fields.code)}" placeholder="${fields.urlHasPwd ? '链接已带 pwd，按规范省略' : '没有可留空'}" ${fields.urlHasPwd ? 'disabled' : ''} autocomplete="off" spellcheck="false">
                            </label>
                            <label class="panai-inbox-field">
                                <span>解压码${extractInfo.source === 'bare' ? '<em class="panai-inbox-src">来自裸「密码」，请确认</em>' : ''}</span>
                                <input type="text" id="Panai-Extract" value="${util.escapeHtml(fields.extract)}" placeholder="没有可留空" autocomplete="off" spellcheck="false">
                            </label>
                        </div>
                        <div class="panai-inbox-link" title="${util.escapeHtml(url)}">${util.escapeHtml(url)}</div>
                        <textarea id="Panai-Preview" class="panai-inbox-textarea" spellcheck="false">${util.escapeHtml(preview)}</textarea>
                        <div id="Panai-Inbox-Tip" class="panai-inbox-tip"></div>
                        <div class="panai-inbox-note">改上面三个字段会重新生成文本；也可以直接改文本框。</div>
                    </div>`;
            } else {
                // Gofile / PikPak：链接本身就是分享，不需要提取码，也没有解压码。
                // 一律默认留空；只有该分享额外设了密码时才手填。
                let warn = pwdShare
                    ? `${ctx.name}：无需提取码 / 解压码，直接发送链接即可。若该分享额外设了密码，可在下面手填。`
                    : `${ctx.name || '非百度网盘'}：只发送链接本身，不拼 RJ / 提取码 / 解压码。服务端收件箱目前只按「百度 / 外链」两类解析。`;
                let codeField = pwdShare ? `
                        <div class="panai-inbox-fields">
                            <label class="panai-inbox-field">
                                <span>分享密码${fields.urlHasPwd ? '（链接已带，无需填写）' : '（一般留空）'}</span>
                                <input type="text" id="Panai-Code" value="${util.escapeHtml(fields.code)}" placeholder="一般留空" ${fields.urlHasPwd ? 'disabled' : ''} autocomplete="off" spellcheck="false">
                            </label>
                        </div>` : '';
                bodyHtml = `
                    <div class="panai-inbox">
                        <div class="panai-inbox-meta">目标：${util.escapeHtml(baseUrl)}</div>
                        <div class="panai-inbox-warn">${util.escapeHtml(warn)}</div>${codeField}
                        <textarea id="Panai-Preview" class="panai-inbox-textarea" spellcheck="false">${util.escapeHtml(preview)}</textarea>
                        <div id="Panai-Inbox-Tip" class="panai-inbox-tip"></div>
                    </div>`;
            }

            self.dialogLock = true;
            Swal.fire({
                title: title,
                html: bodyHtml,
                width: 660,
                showCancelButton: true,
                showCloseButton: true,
                confirmButtonText: channelOn ? '确认发送' : '复制规范文本',
                cancelButtonText: '取消',
                focusConfirm: false,
                customClass,
                didClose: () => {
                    self.dialogLock = false;
                },
                didOpen: () => {
                    let textarea = document.getElementById('Panai-Preview');
                    let tip = document.getElementById('Panai-Inbox-Tip');
                    let rjInput = document.getElementById('Panai-Rj');
                    let codeInput = document.getElementById('Panai-Code');
                    let extractInput = document.getElementById('Panai-Extract');

                    let refreshTip = () => {
                        let result = isBaidu ? self.validateLine(textarea.value) : self.validateLinkOnly(textarea.value);
                        tip.className = 'panai-inbox-tip ' + (result.ok ? 'ok' : 'bad');
                        if (result.ok) {
                            tip.textContent = channelOn
                                ? '格式校验通过，点「确认发送」推送到软件。'
                                : '格式校验通过。推送通道未启用，点「复制规范文本」后可粘到软件输入框。';
                        } else {
                            tip.textContent = result.message;
                        }
                        return result;
                    };

                    let rebuild = () => {
                        if (rjInput) fields.rj = self.normalizeRj(rjInput.value);
                        if (codeInput && !fields.urlHasPwd) {
                            // 这里只清洗不淘汰：把非法值留在行里，让校验明确报错，
                            // 而不是让用户打进去的内容悄悄消失
                            fields.code = self.cleanTransferInput(codeInput.value);
                        }
                        if (extractInput) {
                            fields.extract = self.cleanExtractInput(extractInput.value);
                        }
                        textarea.value = isBaidu
                            ? self.composeLine(fields)
                            : (pwdShare ? self.composeHttpLine(url, fields.code) : url);
                        refreshTip();
                    };

                    [rjInput, codeInput, extractInput].forEach((el) => {
                        el && el.addEventListener('input', rebuild);
                    });
                    textarea.addEventListener('input', refreshTip);

                    refreshTip();
                    if (isBaidu && !fields.rj && rjInput) rjInput.focus();
                },
                preConfirm: () => {
                    let textarea = document.getElementById('Panai-Preview');
                    let text = String(textarea ? textarea.value : '').trim();
                    let check = isBaidu ? self.validateLine(text) : self.validateLinkOnly(text);
                    if (!check.ok) {
                        Swal.showValidationMessage(util.escapeHtml(check.message));
                        return false;
                    }
                    if (!channelOn) {
                        return self.copyToClipboard(text).then((ok) => {
                            if (!ok) {
                                Swal.showValidationMessage('复制失败，请手动选中文本框内容复制');
                                return false;
                            }
                            return {copied: true, text: text};
                        });
                    }
                    let button = Swal.getConfirmButton();
                    if (button) button.disabled = true;
                    return self.requestPush({
                        baseUrl: baseUrl,
                        token: self.getToken(),
                        provider: self.inboxProvider(isBaidu),
                        text: text
                    }).then((res) => {
                        if (!res.ok) {
                            if (button) button.disabled = false;
                            Swal.showValidationMessage(self.describePushError(res));
                            return false;
                        }
                        return res;
                    });
                }
            }).then((res) => {
                if (!res.isConfirmed || !res.value) return;
                self.showPushResult(res.value);
            });
        },

        copyToClipboard(text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                return navigator.clipboard.writeText(text).then(() => true).catch(() => this.copyFallback(text));
            }
            return Promise.resolve(this.copyFallback(text));
        },

        copyFallback(text) {
            try {
                let area = document.createElement('textarea');
                area.value = text;
                area.style.position = 'fixed';
                area.style.top = '-1000px';
                area.style.opacity = '0';
                document.body.appendChild(area);
                area.select();
                let ok = document.execCommand('copy');
                document.body.removeChild(area);
                return !!ok;
            } catch (e) {
                return false;
            }
        },

        // 后端契约：POST {baseUrl}/api/download-inbox/push
        // body: {provider, text, source, client_id}；可选头 X-Inbox-Token
        requestPush(payload) {
            let url = payload.baseUrl + '/api/download-inbox/push';
            let body = JSON.stringify({
                provider: payload.provider,
                text: payload.text,
                source: 'userscript',
                client_id: 'panai'
            });
            let headers = {'Content-Type': 'application/json'};
            let token = String(payload.token || '').trim();
            if (token) headers['X-Inbox-Token'] = token;

            let parse = (status, raw) => {
                let json = null;
                try {
                    json = JSON.parse(raw);
                } catch (e) {
                    json = null;
                }
                return {
                    ok: status >= 200 && status < 300,
                    status: status,
                    raw: String(raw || ''),
                    json: json
                };
            };

            return new Promise((resolve) => {
                if (typeof GM_xmlhttpRequest === 'function') {
                    GM_xmlhttpRequest({
                        method: 'POST',
                        url: url,
                        headers: headers,
                        data: body,
                        timeout: 15000,
                        onload: (res) => resolve(parse(res.status, res.responseText)),
                        onerror: () => resolve({ok: false, status: 0, raw: '无法连接 ' + payload.baseUrl, json: null}),
                        ontimeout: () => resolve({ok: false, status: 0, raw: '请求超时（15 秒）', json: null})
                    });
                    return;
                }
                fetch(url, {method: 'POST', headers: headers, body: body})
                    .then((res) => res.text().then((raw) => resolve(parse(res.status, raw))))
                    .catch((err) => resolve({ok: false, status: 0, raw: String(err && err.message || err), json: null}));
            });
        },

        describePushError(result) {
            let status = result.status;
            let hint = '';
            if (!status) hint = '网络层失败';
            else if (status === 400) hint = '文本被服务端拒绝（单次 ≤200 行、≤256KB）';
            else if (status === 401) hint = '令牌不匹配，检查软件设置里的访问令牌';
            else if (status === 403) hint = '收件箱未启用，或来源不在局域网且未配置令牌';
            else if (status === 404) hint = '接口不存在——后端收件箱尚未发版';
            else if (status === 413) hint = '正文过大（多为反向代理拦截）';
            else hint = '服务端返回异常';
            let detail = result.json && result.json.detail ? String(result.json.detail) : '';
            return `<div style="text-align:left;font-size:12px;">
                        <div style="font-weight:700;color:#d14529;">HTTP ${status || '-'} · ${util.escapeHtml(hint)}</div>
                        ${detail ? `<div style="margin-top:4px;color:#555;">${util.escapeHtml(detail)}</div>` : ''}
                        <pre style="margin:6px 0 0;max-height:110px;overflow:auto;white-space:pre-wrap;word-break:break-all;background:#f6f6f6;padding:6px;border-radius:4px;color:#333;">${util.escapeHtml(result.raw || '')}</pre>
                    </div>`;
        },

        showPushResult(result) {
            if (result.copied) {
                Swal.fire({
                    toast: true,
                    position: 'top',
                    icon: 'success',
                    title: '已复制规范文本，粘到软件输入框即可',
                    timer: 2600,
                    showConfirmButton: false,
                    customClass
                });
                return;
            }
            let json = result.json || {};
            let accepted = Number(json.accepted || 0);
            let duplicated = Number(json.duplicated || 0);
            let target = util.escapeHtml(this.getBaseUrl());
            Swal.fire({
                icon: 'success',
                title: `已送入 KikoeruManager（${accepted} 条，跳过 ${duplicated} 条重复）`,
                html: `<div style="font-size:13px;line-height:20px;color:#888;">回到软件页面的「ASMR 同步下载」点「预览」即可。<br>同一链接重复推送只会更新，不会堆积。<br><a href="${target}" target="_blank" rel="noopener">打开 ${target}</a></div>`,
                confirmButtonText: '好',
                customClass
            });
        },

        // 格式自检：直接跑提示词里的验收用例
        selfTest() {
            let cases = [];
            let check = (label, actual, expected) => {
                cases.push({label, actual: String(actual), expected: String(expected), ok: String(actual) === String(expected)});
            };

            check('① 四段齐全',
                this.composeLine({rj: 'RJ01234567', url: 'https://pan.baidu.com/s/1AbCdEf', urlHasPwd: false, code: 'xywz', extract: 'abc123'}),
                'RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 提取码: xywz | 解压码: abc123');
            check('② 链接带 pwd 省略提取码',
                this.composeLine({rj: 'RJ01234567', url: 'https://pan.baidu.com/s/1AbCdEf?pwd=xywz', urlHasPwd: true, code: 'xywz', extract: 'abc123'}),
                'RJ01234567 | https://pan.baidu.com/s/1AbCdEf?pwd=xywz | 解压码: abc123');
            check('③ 文件名里取解压码',
                this.findExtractCode('RJ01234567 解压码abc123.zip'),
                'abc123');
            check('④ 无解压码',
                this.composeLine({rj: 'RJ01234567', url: 'https://pan.baidu.com/s/1AbCdEf', urlHasPwd: false, code: 'xywz', extract: ''}),
                'RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 提取码: xywz');
            check('⑤ 无 RJ 必须被拦',
                this.validateLine('https://pan.baidu.com/s/1AbCdEf | 提取码: xywz').ok,
                false);
            check('⑥ 标题多个 RJ 取最大',
                this.pickMaxRj('RJ01111111 RJ02999999 合集'),
                'RJ02999999');
            check('⑦ 裸「密码」被拦',
                this.validateLine('RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 密码: xywz').ok,
                false);
            check('⑧ 全角竖线被拦',
                this.validateLine('RJ01234567 ｜ https://pan.baidu.com/s/1AbCdEf').ok,
                false);
            check('⑨ 小写/带空格 RJ 规范化',
                this.normalizeRj(' rj 01234567 '),
                'RJ01234567');
            check('⑩ 提取码不被当解压码',
                this.parseTransferCode('度盘链接：https://pan.baidu.com/s/1S7fZ5loZ0r0Uy1maG9yt2g\n30天捏。\n提取码：2114'),
                '2114');
            check('⑪ 解压密码不进提取码',
                this.parseTransferCode('链接：https://pan.baidu.com/s/1AbCdEf\n解压密码：abc123'),
                '');
            check('⑫ 解压密码只进解压码',
                this.findExtractCode('链接：https://pan.baidu.com/s/1AbCdEf\n解压密码：yd_123'),
                'yd_123');
            check('⑬ 裸「密码」按解压码处理',
                this.resolveExtractCode(['链接：https://pan.baidu.com/s/1AbCdEf\n密码：abc123']).value,
                'abc123');
            check('⑭ 裸「密码」不进提取码',
                this.parseTransferCode('链接：https://pan.baidu.com/s/1AbCdEf\n密码：abc123'),
                '');
            check('⑮ 提取码与裸「密码」各归位',
                this.parseTransferCode('链接：https://pan.baidu.com/s/1AbCdEf\n提取码：2114\n密码：abc123') + '|' + this.resolveExtractCode(['链接：https://pan.baidu.com/s/1AbCdEf\n提取码：2114\n密码：abc123']).value,
                '2114|abc123');
            check('⑯ 访问密码仍算提取码',
                this.parseTransferCode('访问密码：q9w8'),
                'q9w8');
            check('⑰ 解压码带 @ 与中文',
                this.findExtractCode('解压码：southplus@汉末蜡笔'),
                'southplus@汉末蜡笔');
            check('⑱ 带符号解压码能通过校验',
                this.validateLine('RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 提取码: xywz | 解压码: southplus@汉末蜡笔').ok,
                true);
            check('⑲ 解压码里的空格被拦',
                this.validateLine('RJ01234567 | https://pan.baidu.com/s/1AbCdEf | 解压码: south plus').ok,
                false);
            check('⑳ 「解压密码请见下方」不算密码',
                this.findExtractCode('解压密码请见下方'),
                '');

            let failed = cases.filter((item) => !item.ok).length;
            let rows = cases.map((item) => `<div style="display:flex;gap:8px;font-size:12px;line-height:20px;text-align:left;">
                        <span style="color:${item.ok ? '#2e7d32' : '#d14529'};font-weight:700;flex:0 0 14px;">${item.ok ? '✓' : '✗'}</span>
                        <span style="flex:0 0 150px;color:#333;">${util.escapeHtml(item.label)}</span>
                        ${item.ok ? '' : `<code style="flex:1;word-break:break-all;color:#d14529;">${util.escapeHtml(item.actual)} ≠ ${util.escapeHtml(item.expected)}</code>`}
                    </div>`).join('');
            Swal.fire({
                title: failed ? `自检失败 ${failed} 项` : '格式自检全部通过',
                html: `<div>${rows}</div>`,
                width: 660,
                customClass
            });
        },

        registerMenuCommand() {
            GM_registerMenuCommand('👀 已识别：' + util.getValue('setting_success_times') + '次', () => {
                this.clearIdentifyTimes();
            });
            GM_registerMenuCommand(`📋️ 识别剪切板中文字（快捷键 ${util.getValue('setting_hotkeys')}）`, () => {
                this.showIdentifyBox();
            });
            GM_registerMenuCommand('⚙️ 设置', () => {
                this.showSettingBox();
            });
            GM_registerMenuCommand('🧪 收件箱格式自检', () => {
                this.selfTest();
            });
        },

        addPluginStyle() {
            let style = `
                .panai-container { z-index: 99999!important }
                .panai-popup { font-size: 14px !important }
                .panai-setting-label { display: flex;align-items: center;justify-content: space-between;padding-top: 20px; }
                .panai-setting-checkbox { width: 16px;height: 16px; }
                .panai-setting-divider { margin-top: 22px;padding-top: 14px;border-top: 1px dashed #ddd;font-size: 13px;font-weight: 700;color: #666;text-align: left; }
                .panai-setting-hint { margin-top: 10px;font-size: 12px;line-height: 18px;color: #999;text-align: left; }
                .panai-popup.swal2-toast { width: auto; max-width: min(92vw, 480px); }
                .panai-popup.swal2-toast .swal2-actions { flex-wrap: wrap; }
                .panai-inbox { text-align: left; font-size: 14px; }
                .panai-inbox-meta { font-size: 12px; color: #999; margin-bottom: 8px; word-break: break-all; }
                .panai-inbox-warn { font-size: 12px; line-height: 18px; color: #8a6d00; background: #fff8e1; border: 1px solid #ffe0a3; border-radius: 6px; padding: 8px 10px; margin-bottom: 10px; }
                .panai-inbox-fields { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
                .panai-inbox-field { flex: 1 1 30%; display: flex; flex-direction: column; gap: 4px; min-width: 120px; }
                .panai-inbox-field > span { font-size: 12px; color: #666; display: flex; align-items: center; gap: 6px; }
                .panai-inbox-src { font-style: normal; font-size: 11px; color: #aaa; }
                .panai-inbox-field input { box-sizing: border-box; width: 100%; padding: 6px 8px; font-size: 13px; color: #333; background: #fff; border: 1px solid #ccc; border-radius: 4px; }
                .panai-inbox-field input:disabled { background: #f4f4f4; color: #aaa; }
                .panai-inbox-link { font-size: 12px; color: #2778c4; margin-bottom: 8px; word-break: break-all; line-height: 18px; }
                .panai-inbox-textarea { box-sizing: border-box; width: 100%; min-height: 96px; padding: 8px 10px; font-family: Consolas, Menlo, Monaco, monospace; font-size: 12px; line-height: 1.6; color: #333; background: #fff; border: 1px solid #ccc; border-radius: 6px; resize: vertical; }
                .panai-inbox-tip { margin-top: 8px; font-size: 12px; line-height: 18px; min-height: 18px; }
                .panai-inbox-tip.ok { color: #2e7d32; }
                .panai-inbox-tip.bad { color: #d14529; }
                .panai-inbox-note { margin-top: 6px; font-size: 11px; color: #aaa; }
            `;

            if (document.head) {
                util.addStyle('swal-pub-style', 'style', GM_getResourceText('swalStyle'));
                util.addStyle('panai-style', 'style', style);
            }

            const headObserver = new MutationObserver(() => {
                util.addStyle('swal-pub-style', 'style', GM_getResourceText('swalStyle'));
                util.addStyle('panai-style', 'style', style);
            });
            headObserver.observe(document.head, {childList: true, subtree: true});
        },

        isTopWindow() {
            return window.self === window.top;
        },

        init() {
            this.initValue();
            this.addPluginStyle();
            this.addHotKey();
            this.autoFillPassword();
            this.addPageListener();
            this.isTopWindow() && this.registerMenuCommand();
        },
    };

    main.init();
})();
