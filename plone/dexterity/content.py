from Acquisition import Explicit, aq_base, aq_parent
from DateTime import DateTime
from zExceptions import Unauthorized
from OFS.PropertyManager import PropertyManager
from OFS.SimpleItem import SimpleItem

from copy import deepcopy

from zope.component import queryUtility

from zope.interface import implements
from zope.interface.declarations import Implements
from zope.interface.declarations import implementedBy
from zope.interface.declarations import getObjectSpecification
from zope.interface.declarations import ObjectSpecificationDescriptor

from zope.security.interfaces import IPermission

from zope.annotation import IAttributeAnnotatable

from plone.dexterity.interfaces import IDexterityContent
from plone.dexterity.interfaces import IDexterityItem
from plone.dexterity.interfaces import IDexterityContainer

from plone.dexterity.schema import SCHEMA_CACHE
from plone.dexterity.utils import iterSchemata
from zope.schema import getFieldsInOrder

from zope.container.contained import Contained

import AccessControl.Permissions
from AccessControl import ClassSecurityInfo
from AccessControl import getSecurityManager

from Products.CMFCore import permissions
from Products.CMFCore.PortalContent import PortalContent
from Products.CMFCore.PortalFolder import PortalFolderBase
from Products.CMFCore.CMFCatalogAware import CMFCatalogAware
from Products.CMFPlone.interfaces import IConstrainTypes
from Products.CMFCore.interfaces import ITypeInformation
from Products.CMFCore.interfaces import ICatalogableDublinCore
from Products.CMFCore.interfaces import IDublinCore
from Products.CMFCore.interfaces import IMutableDublinCore

from Products.CMFDynamicViewFTI.browserdefault import BrowserDefaultMixin

from plone.folder.ordered import CMFOrderedBTreeFolderBase
from plone.uuid.interfaces import IAttributeUUID
from plone.uuid.interfaces import IUUID

from plone.autoform.interfaces import READ_PERMISSIONS_KEY
from plone.supermodel.utils import mergedTaggedValueDict

from plone.dexterity.filerepresentation import DAVResourceMixin, DAVCollectionMixin
from plone.dexterity.interfaces import IDexterityFTI
from plone.dexterity.utils import datify
from plone.dexterity.utils import safe_utf8
from plone.dexterity.utils import safe_unicode

_marker = object()
_zone = DateTime().timezone()
FLOOR_DATE = DateTime(1970, 0)  # always effective
CEILING_DATE = DateTime(2500, 0)  # never expires


class FTIAwareSpecification(ObjectSpecificationDescriptor):
    """A __providedBy__ decorator that returns the interfaces provided by
    the object, plus the schema interface set in the FTI.
    """

    def __get__(self, inst, cls=None):
        # We're looking at a class - fall back on default
        if inst is None:
            return getObjectSpecification(cls)

        # Find the data we need to know if our cache needs to be invalidated
        direct_spec = getattr(inst, '__provides__', None)
        portal_type = getattr(inst, 'portal_type', None)

        spec = direct_spec

        # If the instance doesn't have a __provides__ attribute, get the
        # interfaces implied by the class as a starting point.
        if spec is None:
            spec = implementedBy(cls)

        # If the instance has no portal type, then we're done.
        if portal_type is None:
            return spec

        fti = queryUtility(IDexterityFTI, name=portal_type)
        if fti is None:
            return spec

        schema = SCHEMA_CACHE.get(portal_type)
        subtypes = SCHEMA_CACHE.subtypes(portal_type)

        # Find the cached value. This calculation is expensive and called
        # hundreds of times during each request, so we require a fast cache
        cache = getattr(inst, '_v__providedBy__', None)
        updated = inst._p_mtime, schema, subtypes, direct_spec

        # See if we have a valid cache. Reasons to do this include:
        #
        #  - The schema was modified.
        #  - The subtypes were modified.
        #  - The instance was modified and persisted since the cache was built.
        #  - The instance has a different direct specification.
        if cache is not None:
            cached_mtime, cached_schema, cached_subtypes, \
                cached_direct_spec, cached_spec = cache

            if cache[:-1] == updated:
                return cached_spec

        dynamically_provided = [] if schema is None else [schema]
        dynamically_provided.extend(subtypes)

        # If we have neither a schema, nor a subtype, then we're also done.
        if not dynamically_provided:
            return spec

        dynamically_provided.append(spec)
        spec = Implements(*dynamically_provided)
        inst._v__providedBy__ = updated + (spec, )

        return spec


class AttributeValidator(Explicit):
    """Decide whether attributes should be accessible. This is set as the
    __allow_access_to_unprotected_subobjects__ variable in Dexterity's content
    classes.
    """

    def __call__(self, name, value):

        # Short circuit for things like views or viewlets
        if name == '':
            return 1

        context = aq_parent(self)

        schema = self._get_schema(context)
        if schema is None:
            return 1

        info = mergedTaggedValueDict(schema, READ_PERMISSIONS_KEY)

        if name not in info:
            return 1

        permission = queryUtility(IPermission, name=info[name])
        if permission is not None:
            return getSecurityManager().checkPermission(permission.title, context)

        return 0

    def _get_schema(self, inst):
        portal_type = getattr(inst, 'portal_type', None)
        if portal_type is not None:
            try:
                return SCHEMA_CACHE.get(portal_type)
            except (ValueError, AttributeError,):
                pass
        return None


class PasteBehaviourMixin(object):
    def _verifyObjectPaste(self, obj, validate_src=True):
        # Extend the paste checks from OFS.CopySupport.CopyContainer
        # (permission checks) and
        # Products.CMFCore.PortalFolder.PortalFolderBase (permission checks and
        # allowed content types) to also ask the FTI if construction is
        # allowed.
        super(PasteBehaviourMixin, self)._verifyObjectPaste(obj, validate_src)
        if validate_src:
            portal_type = getattr(aq_base(obj), 'portal_type', None)
            if portal_type:
                fti = queryUtility(ITypeInformation, name=portal_type)
                if fti is not None and not fti.isConstructionAllowed(self):
                    raise ValueError('You can not add the copied content here.')


class DexterityContent(DAVResourceMixin, PortalContent, PropertyManager, Contained):
    """Base class for Dexterity content
    """
    implements(
        IDexterityContent, IAttributeAnnotatable, IAttributeUUID,
        IDublinCore, ICatalogableDublinCore, IMutableDublinCore)

    __providedBy__ = FTIAwareSpecification()
    __allow_access_to_unprotected_subobjects__ = AttributeValidator()

    security = ClassSecurityInfo()

    # portal_type is set by the add view and/or factory
    portal_type = None

    title = u''
    description = u''
    subject = ()
    creators = ()
    contributors = ()
    effective_date = None
    expiration_date = None
    format = 'text/html'
    language = ''
    rights = ''

    def __init__(
            self,
            id=None, title=_marker, subject=_marker, description=_marker,
            contributors=_marker, effective_date=_marker,
            expiration_date=_marker, format=_marker, language=_marker,
            rights=_marker, **kwargs):

        if id is not None:
            self.id = id
        now = DateTime()
        self.creation_date = now
        self.modification_date = now

        if title is not _marker:
            self.setTitle(title)
        if subject is not _marker:
            self.setSubject(subject)
        if description is not _marker:
            self.setDescription(description)
        if contributors is not _marker:
            self.setContributors(contributors)
        if effective_date is not _marker:
            self.setEffectiveDate(effective_date)
        if expiration_date is not _marker:
            self.setExpirationDate(expiration_date)
        if format is not _marker:
            self.setFormat(format)
        if language is not _marker:
            self.setLanguage(language)
        if rights is not _marker:
            self.setRights(rights)

        for (k, v) in kwargs.items():
            setattr(self, k, v)

    def __getattr__(self, name):
        # optimization: sometimes we're asked for special attributes
        # such as __conform__ that we can disregard (because we
        # wouldn't be in here if the class had such an attribute
        # defined).
        if name.startswith('__'):
            raise AttributeError(name)

        # attribute was not found; try to look it up in the schema and return
        # a default
        schema = SCHEMA_CACHE.get(self.portal_type)
        if schema is not None:
            field = schema.get(name, None)
            if field is not None:
                return deepcopy(field.default)

        # do the same for each subtype
        for schema in SCHEMA_CACHE.subtypes(self.portal_type):
            field = schema.get(name, None)
            if field is not None:
                return deepcopy(field.default)

        raise AttributeError(name)

    # Let __name__ and id be identical. Note that id must be ASCII in Zope 2,
    # but __name__ should be unicode. Note that setting the name to something
    # that can't be encoded to ASCII will throw a UnicodeEncodeError

    def _get__name__(self):
        return unicode(self.id)
    def _set__name__(self, value):
        if isinstance(value, unicode):
            value = str(value) # may throw, but that's OK - id must be ASCII
        self.id = value
    __name__ = property(_get__name__, _set__name__)

    def UID(self):
        """Returns the item's globally unique id."""
        return IUUID(self)

    @security.private
    def notifyModified(self):
        """Update creators and modification_date.

        This is called from CMFCatalogAware.reindexObject.
        """
        self.addCreator()
        self.setModificationDate()

    @security.protected(permissions.ModifyPortalContent)
    def addCreator(self, creator=None):
        """ Add creator to Dublin Core creators.
        """
        if creator is None:
            user = getSecurityManager().getUser()
            creator = user and user.getId()

        # call self.listCreators() to make sure self.creators exists
        if creator and not creator in self.listCreators():
            self.creators = self.creators + (creator, )

    @security.protected(permissions.ModifyPortalContent)
    def setModificationDate(self, modification_date=None):
        """ Set the date when the resource was last modified.

        When called without an argument, sets the date to now.
        """
        if modification_date is None:
            self.modification_date = DateTime()
        else:
            self.modification_date = self._datify(modification_date)

    # IMinimalDublinCore

    @security.protected(permissions.View)
    def Title(self):
        # this is a CMF accessor, so should return utf8-encoded
        if isinstance(self.title, unicode):
            return self.title.encode('utf-8')
        return self.title or ''

    @security.protected(permissions.View)
    def Description(self):
        # this is a CMF accessor, so should return utf8-encoded
        if isinstance(self.description, unicode):
            return self.description.encode('utf-8')
        return self.description or ''

    @security.protected(permissions.View)
    def Type(self):
        ti = self.getTypeInfo()
        return ti is not None and ti.Title() or 'Unknown'

    # IDublinCore

    @security.protected(permissions.View)
    def listCreators(self):
        # List Dublin Core Creator elements - resource authors.
        if self.creators is None:
            return ()
        return tuple(safe_utf8(c) for c in self.creators)

    @security.protected(permissions.View)
    def Creator(self):
        # Dublin Core Creator element - resource author.
        creators = self.listCreators()
        return creators and creators[0] or ''

    @security.protected(permissions.View)
    def Subject(self):
        # Dublin Core Subject element - resource keywords.
        if self.subject is None:
            return ()
        return tuple(safe_utf8(s) for s in self.subject)

    @security.protected(permissions.View)
    def Publisher(self):
        # Dublin Core Publisher element - resource publisher.
        return 'No publisher'

    @security.protected(permissions.View)
    def listContributors(self):
        # Dublin Core Contributor elements - resource collaborators.
        return tuple(safe_utf8(c) for c in self.contributors)

    @security.protected(permissions.View)
    def Contributors(self):
        # Deprecated alias of listContributors.
        return self.listContributors()

    @security.protected(permissions.View)
    def Date(self, zone=None):
        # Dublin Core Date element - default date.
        if zone is None:
            zone = _zone
        # Return effective_date if set, modification date otherwise
        date = getattr(self, 'effective_date', None)
        if date is None:
            date = self.modified()
        return date.toZone(zone).ISO()

    @security.protected(permissions.View)
    def CreationDate(self, zone=None):
        # Dublin Core Date element - date resource created.
        if zone is None:
            zone = _zone
        # return unknown if never set properly
        if self.creation_date:
            return self.creation_date.toZone(zone).ISO()
        else:
            return 'Unknown'

    @security.protected(permissions.View)
    def EffectiveDate(self, zone=None):
        # Dublin Core Date element - date resource becomes effective.
        if zone is None:
            zone = _zone
        ed = getattr(self, 'effective_date', None)
        return ed and ed.toZone(zone).ISO() or 'None'

    @security.protected(permissions.View)
    def ExpirationDate(self, zone=None):
        # Dublin Core Date element - date resource expires.
        if zone is None:
            zone = _zone
        ed = getattr(self, 'expiration_date', None)
        return ed and ed.toZone(zone).ISO() or 'None'

    @security.protected(permissions.View)
    def ModificationDate(self, zone=None):
        # Dublin Core Date element - date resource last modified.
        if zone is None:
            zone = _zone
        return self.modified().toZone(zone).ISO()

    @security.protected(permissions.View)
    def Identifier(self):
        # Dublin Core Identifier element - resource ID.
        return self.absolute_url()

    @security.protected(permissions.View)
    def Language(self):
        # Dublin Core Language element - resource language.
        return self.language

    @security.protected(permissions.View)
    def Rights(self):
        # Dublin Core Rights element - resource copyright.
        return safe_utf8(self.rights)

    # ICatalogableDublinCore

    @security.protected(permissions.View)
    def created(self):
        # Dublin Core Date element - date resource created.
        # allow for non-existent creation_date, existed always
        date = getattr(self, 'creation_date', None)
        return date is None and FLOOR_DATE or date

    @security.protected(permissions.View)
    def effective(self):
        # Dublin Core Date element - date resource becomes effective.
        date = getattr(self, 'effective_date', _marker)
        if date is _marker:
            date = getattr(self, 'creation_date', None)
        return date is None and FLOOR_DATE or date

    @security.protected(permissions.View)
    def expires(self):
        # Dublin Core Date element - date resource expires.
        date = getattr(self, 'expiration_date', None)
        return date is None and CEILING_DATE or date

    @security.protected(permissions.View)
    def modified(self):
        # Dublin Core Date element - date resource last modified.
        date = self.modification_date
        if date is None:
            # Upgrade.
            date = self.bobobase_modification_time()
            self.modification_date = date
        return date

    @security.protected(permissions.View)
    def isEffective(self, date):
        # Is the date within the resource's effective range?
        pastEffective = (
            self.effective_date is None or self.effective_date <= date)
        beforeExpiration = (
            self.expiration_date is None or self.expiration_date >= date)
        return pastEffective and beforeExpiration

    # IMutableDublinCore

    @security.protected(permissions.ModifyPortalContent)
    def setTitle(self, title):
        # Set Dublin Core Title element - resource name.
        self.title = safe_unicode(title)

    @security.protected(permissions.ModifyPortalContent)
    def setDescription(self, description):
        # Set Dublin Core Description element - resource summary.
        self.description = safe_unicode(description)

    @security.protected(permissions.ModifyPortalContent)
    def setCreators(self, creators):
        # Set Dublin Core Creator elements - resource authors.
        if isinstance(creators, basestring):
            creators = [creators]
        self.creators = tuple(safe_unicode(c.strip()) for c in creators)

    @security.protected(permissions.ModifyPortalContent)
    def setSubject(self, subject):
        # Set Dublin Core Subject element - resource keywords.
        if isinstance(subject, basestring):
            subject = [subject]
        self.subject = tuple(safe_unicode(s.strip()) for s in subject)

    @security.protected(permissions.ModifyPortalContent)
    def setContributors(self, contributors):
        # Set Dublin Core Contributor elements - resource collaborators.
        if isinstance(contributors, basestring):
            contributors = contributors.split(';')
        self.contributors = tuple(
            safe_unicode(c.strip()) for c in contributors)

    @security.protected(permissions.ModifyPortalContent)
    def setEffectiveDate(self, effective_date):
        # Set Dublin Core Date element - date resource becomes effective.
        self.effective_date = datify(effective_date)

    @security.protected(permissions.ModifyPortalContent)
    def setExpirationDate(self, expiration_date):
        # Set Dublin Core Date element - date resource expires.
        self.expiration_date = datify(expiration_date)

    @security.protected(permissions.ModifyPortalContent)
    def setFormat(self, format):
        # Set Dublin Core Format element - resource format.
        self.format = format

    @security.protected(permissions.ModifyPortalContent)
    def setLanguage(self, language):
        # Set Dublin Core Language element - resource language.
        self.language = language

    @security.protected(permissions.ModifyPortalContent)
    def setRights(self, rights):
        # Set Dublin Core Rights element - resource copyright.
        self.rights = safe_unicode(rights)

    def getField(self, name):
        """Given a field name, return a field instance. Party hard.
        """
        fields = self.getFields()
        for field in fields:
            if field.getName() == name:
                return field
        return None

    def getFields(self):
        """Return all fields for this content type, as field instances.
        Because of behaviors, fields are distributed across several
        schemata. Fields will be returned in proper order.
        """
        fields = []
        for schemata in iterSchemata(self):
            fieldsInOrder = getFieldsInOrder(schemata)
            # TODO: once python 2.6 support is out, make this an OrderedDict
            for orderedField in fieldsInOrder:
                fields.append(orderedField[-1])
        return fields

    def getFieldNames(self):
        """Return a list of the names of the fields. Just some convenience
        cause I love yo faces!
        """
        return self.asDictionary().keys()

    def asDictionary(self, checkConstraints=False):
        """Return a dictionary of key, value pairs of all fields.
        If checkContraints is True, it will onyl return values
        that the authenticated user is allowed to see. Otherwise,
        all attribute,value pairs are returned.
        """
        hotness = {}  # pep8
        fields = self.getFields()
        for field in fields:
            if checkConstraints:
                if not self.canViewField(field):
                    continue
            hotness[field.getName()] = self.getValue(field)
        return hotness

    def canViewField(self, field):
        """returns True if the logged in user has permission to view this
        field
        """
        info = mergedTaggedValueDict(field.interface, READ_PERMISSIONS_KEY)

        # If there is no specific read permission, assume it is view
        if field not in info:
            return getSecurityManager().checkPermission(AccessControl.Permissions.view,
                                                        self)

        permission = queryUtility(IPermission, name=info[field])
        if permission is not None:
            return getSecurityManager().checkPermission(permission.title, self)

        return False

    def getValue(self, field):
        """While it may seem like you should just be able to access
        this contents attributes, this is not true :|. If something
        is provided as an adapter the adapter must be applied to get
        the actual field value. We can't use get() because Container
        overrides it to get subitems. So we use this obscure interface
        syntax instead of looking up and adapting the schema.

        Begin face exploding sequence in 3,2,1...
        """
        behaviorAdapter = field.interface(self)
        return getattr(behaviorAdapter, field.getName())


class Item(PasteBehaviourMixin, BrowserDefaultMixin, DexterityContent):
    """A non-containerish, CMFish item
    """

    implements(IDexterityItem)
    __providedBy__ = FTIAwareSpecification()
    __allow_access_to_unprotected_subobjects__ = AttributeValidator()

    isPrincipiaFolderish = 0

    manage_options = PropertyManager.manage_options + ({
        'label': 'View',
        'action': 'view',
        },) + CMFCatalogAware.manage_options + SimpleItem.manage_options

    # Be explicit about which __getattr__ to use
    __getattr__ = DexterityContent.__getattr__


class Container(
        PasteBehaviourMixin, DAVCollectionMixin, BrowserDefaultMixin,
        CMFCatalogAware, CMFOrderedBTreeFolderBase, DexterityContent):
    """Base class for folderish items
    """

    implements(IDexterityContainer)
    __providedBy__ = FTIAwareSpecification()
    __allow_access_to_unprotected_subobjects__ = AttributeValidator()

    security = ClassSecurityInfo()
    security.declareProtected(
        AccessControl.Permissions.copy_or_move, 'manage_copyObjects')
    security.declareProtected(
        permissions.ModifyPortalContent, 'manage_cutObjects')
    security.declareProtected(
        permissions.ModifyPortalContent, 'manage_pasteObjects')
    security.declareProtected(
        permissions.ModifyPortalContent, 'manage_renameObject')
    security.declareProtected(
        permissions.ModifyPortalContent, 'manage_renameObjects')

    isPrincipiaFolderish = 1

    # make sure CMFCatalogAware's manage_options don't take precedence
    manage_options = PortalFolderBase.manage_options

    # Make sure PortalFolder's accessors and mutators don't take precedence
    Title = DexterityContent.Title
    setTitle = DexterityContent.setTitle
    Description = DexterityContent.Description
    setDescription = DexterityContent.setDescription

    def __init__(self, id=None, **kwargs):
        CMFOrderedBTreeFolderBase.__init__(self, id)
        DexterityContent.__init__(self, id, **kwargs)

    def __getattr__(self, name):
        try:
            return DexterityContent.__getattr__(self, name)
        except AttributeError:
            pass

        # Be specific about the implementation we use
        return CMFOrderedBTreeFolderBase.__getattr__(self, name)

    @security.protected(permissions.DeleteObjects)
    def manage_delObjects(self, ids=None, REQUEST=None):
        """Delete the contained objects with the specified ids.

        If the current user does not have permission to delete one of the
        objects, an Unauthorized exception will be raised.
        """
        if ids is None:
            ids = []
        if isinstance(ids, basestring):
            ids = [ids]
        for id in ids:
            item = self._getOb(id)
            if not getSecurityManager().checkPermission(permissions.DeleteObjects, item):
                raise Unauthorized, (
                    "Do not have permissions to remove this object")
        return super(Container, self).manage_delObjects(ids, REQUEST=REQUEST)

    # override PortalFolder's allowedContentTypes to respect IConstrainTypes
    # adapters
    def allowedContentTypes(self, context=None):
        if not context:
            context = self

        constrains = IConstrainTypes(context, None)
        if not constrains:
            return super(Container, self).allowedContentTypes()

        return constrains.allowedContentTypes()

    # override PortalFolder's invokeFactory to respect IConstrainTypes
    # adapters
    def invokeFactory(self, type_name, id, RESPONSE=None, *args, **kw):
        """Invokes the portal_types tool
        """
        constrains = IConstrainTypes(self, None)

        if constrains and not type_name in [fti.getId() for fti in constrains.allowedContentTypes()]:
            raise ValueError('Subobject type disallowed by IConstrainTypes adapter: %s' % type_name)

        return super(Container, self).invokeFactory(type_name, id, RESPONSE, *args, **kw)


def reindexOnModify(content, event):
    """When an object is modified, re-index it in the catalog
    """

    if event.object is not content:
        return

    # NOTE: We are not using event.descriptions because the field names may
    # not match index names.

    content.reindexObject()
