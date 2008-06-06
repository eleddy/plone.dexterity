from zope.interface import implements

from plone.dexterity.interfaces import IDexterityItem
from plone.dexterity.interfaces import IDexterityContainer

from zope.app.container.contained import Contained

from Products.CMFCore.PortalContent import PortalContent
from Products.CMFCore.CMFCatalogAware import CMFCatalogAware

from Products.CMFDefault.DublinCore import DefaultDublinCoreImpl

from plone.folder.ordered import OrderedBTreeFolderBase

# XXX: It'd be nice to reduce the number of base classes here

class Item(PortalContent, DefaultDublinCoreImpl, Contained):
    """A non-containerish, CMFish item
    """
    
    implements(IDexterityItem)
    isPrincipiaFolderish = 0
    
    # portal_type must be set by factory or derived class
    portal_type = None 
    
    def __init__(self, id=None, **kwargs):
        PortalContent.__init__(self, id, **kwargs)
        DefaultDublinCoreImpl.__init__(self, **kwargs)
        
        if id is not None:
            self.id = id

class Container(CMFCatalogAware, OrderedBTreeFolderBase, PortalContent, DefaultDublinCoreImpl, Contained):
    """Base class for folderish items
    """
    
    implements(IDexterityContainer)
    isPrincipiaFolderish = 1
    
    # portal_type must be set by factory or derived class
    portal_type = None
    
    def __init__(self, id=None, **kwargs):
        OrderedBTreeFolderBase.__init__(self, id, **kwargs)
        DefaultDublinCoreImpl.__init__(self, **kwargs)
        
        if id is not None:
            self.id = id

def reindexOnModify(content, event):
    """When an object is modified, re-index it in the catalog
    """
    
    if event.object is not content:
        return
        
    content.reindexObject(idxs=getattr(event, 'descriptions', []))