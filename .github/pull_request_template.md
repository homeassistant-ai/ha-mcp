## 📋 Pull Request Checklist

### **Type of Change**
- [ ] 🐛 Bug fix (non-breaking change which fixes an issue)
- [ ] ✨ New feature (non-breaking change which adds functionality)
- [ ] 💥 Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] 📚 Documentation update
- [ ] 🔧 Maintenance (dependency updates, refactoring, etc.)

### **Testing** ✅
- [ ] **Integration tests pass**: All Python versions (3.11, 3.12, 3.13)
- [ ] **E2E tests pass**: Testcontainers suite with optimal n=2 workers
- [ ] **Security scan clean**: No critical vulnerabilities introduced
- [ ] **Performance impact**: Acceptable or improved
- [ ] **Local testing**: Manually verified key functionality

### **Code Quality** 🔍
- [ ] **Code follows style guidelines**: Black, Ruff, isort pass
- [ ] **Type checking passes**: MyPy validation clean
- [ ] **Documentation updated**: If functionality changed
- [ ] **Commit messages**: Follow conventional commit format
- [ ] **No secrets exposed**: API keys, tokens, credentials

### **CI/CD Pipeline** 🚀
- [ ] **All workflows pass**: Green checkmarks on all required checks
- [ ] **E2E results consistent**: Expected test counts (37 passed, 1 skipped)
- [ ] **Performance baseline**: No significant regression (>20% slower)
- [ ] **Auto-merge ready**: If safe, add `auto-merge` label

### **Description**
Brief description of changes and motivation:

<!-- Describe your changes here -->

### **Related Issues**
Closes #<!-- issue number -->

### **Performance Impact**
<!-- If applicable, describe performance implications -->
- [ ] No performance impact
- [ ] Performance improved
- [ ] Performance impact documented and acceptable

### **Breaking Changes**
<!-- If applicable, describe any breaking changes -->
- [ ] No breaking changes
- [ ] Breaking changes documented in CHANGELOG

### **Additional Notes**
<!-- Any additional information, deployment notes, etc. -->

---

**For Maintainers:**
- [ ] Ready to merge after CI passes
- [ ] Squash merge recommended
- [ ] Documentation/changelog updates needed