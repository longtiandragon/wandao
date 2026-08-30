(function (root) {
  function normalizedPluginId(value) {
    return String(value || '').trim();
  }

  function isPluginUpdateAvailable(plugin) {
    return Boolean(
      plugin
      && plugin.updateAvailable
      && plugin.compatibility?.compatible !== false
      && normalizedPluginId(plugin.id)
    );
  }

  function availablePluginUpdates(plugins) {
    const seen = new Set();
    return (Array.isArray(plugins) ? plugins : []).filter((plugin) => {
      const id = normalizedPluginId(plugin?.id);
      if (!isPluginUpdateAvailable(plugin) || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
  }

  function platformPluginIds(group) {
    const seen = new Set();
    return (Array.isArray(group?.providers) ? group.providers : [])
      .map((provider) => normalizedPluginId(provider?.pluginId))
      .filter((pluginId) => {
        if (!pluginId || seen.has(pluginId)) return false;
        seen.add(pluginId);
        return true;
      });
  }

  function platformUpdateCandidates(group, plugins) {
    const platformPluginIdSet = new Set(platformPluginIds(group));
    if (!platformPluginIdSet.size) return [];
    return availablePluginUpdates(plugins)
      .filter((plugin) => platformPluginIdSet.has(normalizedPluginId(plugin.id)));
  }

  const api = Object.freeze({
    availablePluginUpdates,
    isPluginUpdateAvailable,
    platformPluginIds,
    platformUpdateCandidates
  });

  root.WandaoPluginUpdates = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
