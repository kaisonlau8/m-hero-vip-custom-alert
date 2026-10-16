/* feedback.js — Ant Design 交互反馈（无构建、无第三方动画库）
 * Button loading、Spin、Message、Notification。页面脚本通过 window.AntFeedback 调用。
 * 本控制台没有上传入口，不提供 Upload 进度。 */
(function () {
  'use strict';

  var ICONS = {
    success: 'i-check-circle',
    error: 'i-close-circle',
    warning: 'i-exclamation-circle',
    info: 'i-info-circle',
    loading: 'i-loading'
  };

  function svgIcon(id, spin) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ai' + (spin ? ' anticon-spin' : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + id);
    svg.appendChild(use);
    return svg;
  }

  function createSpin(size, withTip) {
    var span = document.createElement('span');
    span.className = 'ant-spin ant-spin-spinning' + (size ? ' ant-spin-' + size : '');
    span.setAttribute('role', 'status');
    span.setAttribute('aria-label', '加载中');
    var dot = document.createElement('span');
    dot.className = 'ant-spin-dot ant-spin-dot-spin';
    for (var i = 0; i < 4; i++) {
      var item = document.createElement('i');
      item.className = 'ant-spin-dot-item';
      dot.appendChild(item);
    }
    span.appendChild(dot);
    if (withTip) {
      var text = document.createElement('div');
      text.className = 'ant-spin-text';
      text.textContent = '加载中';
      span.appendChild(text);
    }
    return span;
  }

  function mount(root) {
    var scope = root || document;
    if (!scope.querySelectorAll) return;
    scope.querySelectorAll('[data-af-placeholder-spin]').forEach(function (el) {
      if (el.getAttribute('data-af-spin-done') === '1') return;
      if (el.querySelector('.ant-spin')) return;
      var size = el.getAttribute('data-af-placeholder-spin') || 'sm';
      el.insertBefore(createSpin(size), el.firstChild);
    });
    scope.querySelectorAll('.ant-spin-nested-loading.is-spinning').forEach(function (host) {
      if (host.querySelector(':scope > .ant-spin')) return;
      host.insertBefore(createSpin('', true), host.firstChild);
    });
  }

  function setNestedSpin(el, on) {
    if (!el) return;
    if (on) {
      el.classList.add('is-spinning');
      if (!el.querySelector(':scope > .ant-spin')) el.insertBefore(createSpin('', true), el.firstChild);
    } else {
      el.classList.remove('is-spinning');
    }
  }

  function setButtonLoading(btn, on, deferDisable) {
    if (!btn) return;
    if (on) {
      if (btn.classList.contains('ant-btn-loading')) return;
      btn.dataset.afPrevDisabled = btn.disabled ? '1' : '0';
      btn.classList.add('ant-btn-loading');
      btn.setAttribute('aria-busy', 'true');
      var icon = document.createElement('span');
      icon.className = 'ant-btn-loading-icon';
      icon.appendChild(svgIcon('i-loading', true));
      btn.insertBefore(icon, btn.firstChild);
      if (deferDisable) {
        setTimeout(function () {
          if (btn.isConnected && btn.classList.contains('ant-btn-loading')) btn.disabled = true;
        }, 0);
      } else {
        btn.disabled = true;
      }
      return;
    }
    if (!btn.classList.contains('ant-btn-loading')) return;
    btn.classList.remove('ant-btn-loading');
    btn.removeAttribute('aria-busy');
    var existing = btn.querySelector('.ant-btn-loading-icon');
    if (existing) existing.remove();
    btn.disabled = btn.dataset.afPrevDisabled === '1';
    delete btn.dataset.afPrevDisabled;
  }

  function ensureHolder(className) {
    var el = document.querySelector('.' + className);
    if (!el) {
      el = document.createElement('div');
      el.className = className;
      document.body.appendChild(el);
    }
    return el;
  }

  function removeLater(node, className) {
    node.classList.add(className);
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 200);
  }

  function messageOpen(type, text, duration) {
    var holder = ensureHolder('ant-message');
    var notice = document.createElement('div');
    notice.className = 'ant-message-notice';
    var content = document.createElement('div');
    content.className = 'ant-message-notice-content ant-message-' + type;
    content.setAttribute('role', type === 'error' ? 'alert' : 'status');
    var iconWrap = document.createElement('span');
    iconWrap.className = 'ant-message-icon';
    iconWrap.appendChild(svgIcon(ICONS[type] || ICONS.info, type === 'loading'));
    var span = document.createElement('span');
    span.className = 'ant-message-text';
    span.textContent = text == null ? '' : String(text);
    content.appendChild(iconWrap);
    content.appendChild(span);
    notice.appendChild(content);
    holder.appendChild(notice);
    var ms = duration == null ? (type === 'loading' ? 0 : 3000) : duration;
    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      removeLater(notice, 'ant-message-notice-leave');
    }
    if (ms > 0) setTimeout(close, ms);
    return close;
  }

  function notificationOpen(opts) {
    opts = opts || {};
    var type = opts.type || 'info';
    var holder = ensureHolder('ant-notification');
    var notice = document.createElement('div');
    notice.className = 'ant-notification-notice ant-notification-notice-' + type;
    notice.setAttribute('role', type === 'error' ? 'alert' : 'status');

    var icon = document.createElement('span');
    icon.className = 'ant-notification-notice-icon';
    icon.appendChild(svgIcon(ICONS[type] || ICONS.info, false));

    var title = document.createElement('div');
    title.className = 'ant-notification-notice-message';
    title.textContent = opts.title == null ? '' : String(opts.title);

    var desc = document.createElement('div');
    desc.className = 'ant-notification-notice-description';
    desc.textContent = opts.description == null ? '' : String(opts.description);

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'ant-notification-notice-close';
    closeBtn.setAttribute('aria-label', '关闭');
    closeBtn.appendChild(svgIcon('i-close', false));

    notice.appendChild(icon);
    notice.appendChild(title);
    if (opts.description) notice.appendChild(desc);
    notice.appendChild(closeBtn);
    holder.appendChild(notice);

    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      removeLater(notice, 'ant-notification-notice-leave');
    }
    closeBtn.addEventListener('click', close);
    var ms = opts.duration == null ? 4500 : opts.duration;
    if (ms > 0) setTimeout(close, ms);
    return close;
  }

  function flash(type, text) {
    try {
      sessionStorage.setItem('ant-feedback-flash', JSON.stringify({
        type: type || 'success',
        text: text == null ? '' : String(text)
      }));
    } catch (err) { /* 隐私模式可忽略 */ }
  }

  function consumePending() {
    try {
      var raw = sessionStorage.getItem('ant-feedback-flash');
      if (raw) {
        sessionStorage.removeItem('ant-feedback-flash');
        var data = JSON.parse(raw);
        if (data && data.text) messageOpen(data.type || 'success', data.text);
      }
    } catch (err) { /* 忽略损坏的 flash */ }
    document.querySelectorAll('[data-af-toast]').forEach(function (el) {
      var type = el.getAttribute('data-af-toast') || 'info';
      var text = el.getAttribute('data-af-toast-text') || '';
      if (!text) return;
      var mode = el.getAttribute('data-af-toast-mode') || 'message';
      if (mode === 'notification') {
        notificationOpen({
          type: type,
          title: el.getAttribute('data-af-toast-title') || (type === 'error' ? '操作失败' : '提示'),
          description: text
        });
      } else {
        messageOpen(type, text);
      }
      el.removeAttribute('data-af-toast');
    });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.hasAttribute || !form.hasAttribute('data-af-submit')) return;
    if (e.defaultPrevented) return;
    if (form.dataset.afSubmitted === '1') {
      e.preventDefault();
      return;
    }
    form.dataset.afSubmitted = '1';
    var btn = form.querySelector('button[type="submit"], button:not([type])');
    /* 先换上 loading 外观；disabled 延后一拍，避免部分浏览器取消这次提交 */
    setButtonLoading(btn, true, true);
  });

  function boot() {
    mount(document);
    consumePending();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  function typedMessage(type) {
    return function (text, duration) { return messageOpen(type, text, duration); };
  }
  function typedNotice(type) {
    return function (opts) {
      opts = opts || {};
      opts.type = type;
      return notificationOpen(opts);
    };
  }

  window.AntFeedback = {
    mount: mount,
    createSpin: createSpin,
    setNestedSpin: setNestedSpin,
    setButtonLoading: setButtonLoading,
    flash: flash,
    message: {
      open: messageOpen,
      success: typedMessage('success'),
      error: typedMessage('error'),
      info: typedMessage('info'),
      warning: typedMessage('warning'),
      loading: function (text, duration) { return messageOpen('loading', text, duration == null ? 0 : duration); }
    },
    notification: {
      open: notificationOpen,
      success: typedNotice('success'),
      error: typedNotice('error'),
      info: typedNotice('info'),
      warning: typedNotice('warning')
    }
  };
})();
