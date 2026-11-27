#include "cutils.hpp"

#include <qdir.h>
#include <qfile.h>
#include <qfileinfo.h>
#include <qloggingcategory.h>
#include <qmetaobject.h>
#include <qqmlengine.h>
#include <qquickitemgrabresult.h>
#include <qquickwindow.h>
#include <qregularexpression.h>
#include <qtconcurrentrun.h>

#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "util/metaenum.hpp"

namespace {

Q_LOGGING_CATEGORY(lcCUtils, "helysium.cutils", QtInfoMsg)

bool saveImageSecurely(const QImage& img, const QString& file) {
    if (file.isEmpty() || !file.startsWith(u'/') || file.endsWith(u'/') || file.contains(u"//") ||
        file.contains(u'\0')) {
        return false;
    }

    const QStringList parts = file.split(u'/', Qt::SkipEmptyParts);
    if (parts.size() < 2 || parts.contains(u".") || parts.contains(u"..")) {
        return false;
    }

    const uid_t euid = ::geteuid();
    int dirFd = ::open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (dirFd < 0) {
        return false;
    }

    for (qsizetype i = 0; i < parts.size() - 1; ++i) {
        const QByteArray seg = parts[i].toUtf8();
        int nextFd = ::openat(dirFd, seg.constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (nextFd < 0 && errno == ENOENT) {
            if (::mkdirat(dirFd, seg.constData(), 0700) != 0 && errno != EEXIST) {
                ::close(dirFd);
                return false;
            }
            nextFd = ::openat(dirFd, seg.constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        }
        ::close(dirFd);
        if (nextFd < 0) {
            return false;
        }
        dirFd = nextFd;

        struct stat st{};
        if (::fstat(dirFd, &st) != 0 || !S_ISDIR(st.st_mode)) {
            ::close(dirFd);
            return false;
        }
        if (st.st_uid == euid && (st.st_mode & S_IWOTH) != 0) {
            ::close(dirFd);
            return false;
        }
    }

    struct stat dirSt{};
    if (::fstat(dirFd, &dirSt) != 0 || !S_ISDIR(dirSt.st_mode) || dirSt.st_uid != euid ||
        (dirSt.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        ::close(dirFd);
        return false;
    }

    const QByteArray fileName = parts.last().toUtf8();
    const int fd = ::openat(dirFd, fileName.constData(), O_CREAT | O_EXCL | O_NOFOLLOW | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) {
        ::close(dirFd);
        return false;
    }

    if (::fchmod(fd, S_IRUSR | S_IWUSR) != 0) {
        ::close(fd);
        ::unlinkat(dirFd, fileName.constData(), 0);
        ::close(dirFd);
        return false;
    }

    QFile outFile;
    if (!outFile.open(fd, QIODevice::WriteOnly, QFileDevice::AutoCloseHandle)) {
        ::close(fd);
        ::unlinkat(dirFd, fileName.constData(), 0);
        ::close(dirFd);
        return false;
    }

    const bool ok = img.save(&outFile, "PNG");
    outFile.close();
    if (!ok) {
        ::unlinkat(dirFd, fileName.constData(), 0);
        ::close(dirFd);
        return false;
    }

    ::close(dirFd);
    return true;
}

bool verifySecureSystemFile(const QString& path) {
    if (path.isEmpty() || !path.startsWith(u'/') || path.endsWith(u'/') || path.contains(u"//") ||
        path.contains(u'\0')) {
        return false;
    }

    const QStringList parts = path.split(u'/', Qt::SkipEmptyParts);
    if (parts.size() < 2 || parts.contains(u".") || parts.contains(u"..")) {
        return false;
    }

    int dirFd = ::open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (dirFd < 0) {
        return false;
    }

    struct stat rootSt{};
    if (::fstat(dirFd, &rootSt) != 0 || !S_ISDIR(rootSt.st_mode) || rootSt.st_uid != 0 ||
        (rootSt.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        ::close(dirFd);
        return false;
    }

    for (qsizetype i = 0; i < parts.size() - 1; ++i) {
        const QByteArray seg = parts[i].toUtf8();
        const int nextFd = ::openat(dirFd, seg.constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        ::close(dirFd);
        if (nextFd < 0) {
            return false;
        }
        dirFd = nextFd;

        struct stat st{};
        if (::fstat(dirFd, &st) != 0 || !S_ISDIR(st.st_mode) || st.st_uid != 0 ||
            (st.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
            ::close(dirFd);
            return false;
        }
    }

    const QByteArray fileName = parts.last().toUtf8();
    const int fileFd = ::openat(dirFd, fileName.constData(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    ::close(dirFd);
    if (fileFd < 0) {
        return false;
    }

    struct stat fileSt{};
    const bool ok = (::fstat(fileFd, &fileSt) == 0 && S_ISREG(fileSt.st_mode) && fileSt.st_uid == 0 &&
                     (fileSt.st_mode & (S_IWGRP | S_IWOTH)) == 0);
    ::close(fileFd);
    return ok;
}

} // namespace

namespace helysium::shell {

void CUtils::dependency(const QVariant& value) {
    Q_UNUSED(value);
}

void CUtils::saveItem(QQuickItem* target, const QUrl& path, const QJSValue& onSaved, const QJSValue& onFailed) {
    this->saveItem(target, path, QRect(), onSaved, onFailed);
}

void CUtils::saveItem(
    QQuickItem* target, const QUrl& path, const QRect& rect, const QJSValue& onSaved, const QJSValue& onFailed) {
    if (!target) {
        qCWarning(lcCUtils) << "saveItem: a target is required";
        return;
    }

    if (!path.isLocalFile()) {
        qCWarning(lcCUtils) << "saveItem:" << path << "is not a local file";
        return;
    }

    if (!target->window()) {
        qCWarning(lcCUtils) << "saveItem: unable to save target" << target << "without a window";
        return;
    }

    auto scaledRect = rect;
    const auto scale = target->window()->devicePixelRatio();
    if (rect.isValid() && !qFuzzyCompare(scale + 1.0, 2.0)) {
        scaledRect =
            QRectF(rect.left() * scale, rect.top() * scale, rect.width() * scale, rect.height() * scale).toRect();
    }

    const auto grabResult = target->grabToImage();
    if (!grabResult) {
        qCWarning(lcCUtils) << "saveItem: failed to grab" << target;
        return;
    }

    QObject::connect(
        grabResult.data(), &QQuickItemGrabResult::ready, this,
        [grabResult, scaledRect, path, onSaved, onFailed, this] {
            QtConcurrent::run([image = grabResult->image(), scaledRect, file = path.toLocalFile()] {
                const auto img = scaledRect.isValid() ? image.copy(scaledRect) : image;
                return saveImageSecurely(img, file);
            }).then(this, [path, onSaved, onFailed](bool ok) {
                const auto* cb = ok ? &onSaved : &onFailed;
                if (!ok)
                    qCWarning(lcCUtils) << "saveItem: failed to save" << path;

                if (cb->isCallable())
                    cb->call({ path.toLocalFile() });
            });
        },
        Qt::SingleShotConnection);
}

bool CUtils::copyFile(const QUrl& source, const QUrl& target, bool overwrite) {
    if (!source.isLocalFile()) {
        qCWarning(lcCUtils) << "copyFile: source" << source << "is not a local file";
        return false;
    }
    if (!target.isLocalFile()) {
        qCWarning(lcCUtils) << "copyFile: target" << target << "is not a local file";
        return false;
    }

    if (overwrite && QFile::exists(target.toLocalFile()) && !QFile::remove(target.toLocalFile())) {
        qCWarning(lcCUtils) << "copyFile: overwrite was specified but failed to remove" << target.toLocalFile();
        return false;
    }

    return QFile::copy(source.toLocalFile(), target.toLocalFile());
}

bool CUtils::deleteFile(const QUrl& path) {
    if (!path.isLocalFile()) {
        qCWarning(lcCUtils) << "deleteFile: path" << path << "is not a local file";
        return false;
    }

    return QFile::remove(path.toLocalFile());
}

bool CUtils::isSecureSystemFile(const QString& path) {
    return verifySecureSystemFile(path);
}

QString CUtils::toLocalFile(const QUrl& url) {
    if (!url.isLocalFile()) {
        qCWarning(lcCUtils) << "toLocalFile: given url is not a local file" << url;
        return {};
    }

    return url.toLocalFile();
}

qreal CUtils::clamp(qreal value, qreal min, qreal max) {
    return qBound(min, value, max);
}

QString CUtils::enumToString(QObject* target, const QString& property, const QVariant& value) {
    if (!target) {
        qCWarning(lcCUtils) << "enumToString: a target is required";
        return {};
    }

    const auto* meta = target->metaObject();
    const auto index = meta->indexOfProperty(property.toUtf8().constData());
    if (index < 0) {
        qCWarning(lcCUtils) << "enumToString:" << target << "has no property" << property;
        return {};
    }

    const auto prop = meta->property(index);
    const auto metaEnum = prop.isEnumType() ? prop.enumerator() : util::metaEnumFor(prop.metaType());
    if (!metaEnum.isValid() || metaEnum.is64Bit()) {
        qCWarning(lcCUtils) << "enumToString: property" << property << "of" << target << "is not a supported enum";
        return {};
    }

    const auto val = value.isValid() ? value : prop.read(target);
    const auto* key = util::enumKeyFor(metaEnum, val);
    if (!key) {
        qCWarning(lcCUtils, "enumToString: no enumerator of %s::%s has the value %lld", metaEnum.scope(),
            metaEnum.name(), val.toLongLong());
        return {};
    }

    return QString::fromUtf8(key);
}

namespace {

// DFS over the visual item tree (childItems), returning the first descendant matching the predicate. Unlike
// QObject::findChild, this walks parentItem/childItems relationships so it traverses the QML visual hierarchy.
template <typename Predicate> QQuickItem* findChildDfs(QQuickItem* root, Predicate&& match) {
    const auto children = root->childItems();
    for (QQuickItem* const child : children) {
        if (match(child)) {
            return child;
        }
        if (QQuickItem* const found = findChildDfs(child, match)) {
            return found;
        }
    }
    return nullptr;
}

// DFS over the visual item tree, appending every descendant matching the predicate to out.
template <typename Predicate> void findChildrenDfs(QQuickItem* root, Predicate&& match, QList<QQuickItem*>& out) {
    const auto children = root->childItems();
    for (QQuickItem* const child : children) {
        if (match(child)) {
            out.append(child);
        }
        findChildrenDfs(child, match, out);
    }
}

} // namespace

QQuickItem* CUtils::findChild(QQuickItem* root, const QString& name) {
    if (!root) {
        return nullptr;
    }

    return findChildDfs(root, [&name](const QQuickItem* item) {
        return item->objectName() == name;
    });
}

QList<QQuickItem*> CUtils::findChildren(QQuickItem* root, const QString& name) {
    QList<QQuickItem*> children;
    if (root) {
        findChildrenDfs(
            root,
            [&name](const QQuickItem* item) {
                return item->objectName() == name;
            },
            children);
    }
    return children;
}

QList<QQuickItem*> CUtils::findChildrenMatching(QQuickItem* root, const QString& pattern) {
    QList<QQuickItem*> children;
    if (root) {
        const QRegularExpression re(pattern);
        findChildrenDfs(
            root,
            [&re](const QQuickItem* item) {
                return re.match(item->objectName()).hasMatch();
            },
            children);
    }
    return children;
}

#ifndef HELYSIUM_VERSION
#define HELYSIUM_VERSION ""
#endif

QString CUtils::version() {
    return QStringLiteral(HELYSIUM_VERSION);
}

QString CUtils::qtVersion() {
    return QStringLiteral(QT_VERSION_STR);
}

} // namespace helysium::shell
