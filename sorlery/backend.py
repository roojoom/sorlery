from sorl.thumbnail.conf import settings, defaults as default_settings
from sorl.thumbnail.helpers import tokey, serialize
from sorl.thumbnail.images import DummyImageFile, ImageFile
from sorl.thumbnail.kvstores.base import add_prefix
from sorl.thumbnail.helpers import serialize, deserialize
from sorl.thumbnail import default
from sorl.thumbnail.parsers import parse_geometry
from sorl.thumbnail.base import ThumbnailBackend

from sorlery.tasks import create_thumbnail
import logging

logger = logging.getLogger(__name__)


class QueuedThumbnailBackend(ThumbnailBackend):
    """
    Queue thumbnail generation with django-celery.
    """
    def get_thumbnail(self, file_, geometry_string, **options):
        # Correct way for create source image, This is coped from begining of
        # ThumbnailBackend.get_thumbnail
        if file_:
            source = ImageFile(file_)
        elif settings.THUMBNAIL_DUMMY:
            return DummyImageFile(geometry_string)
        else:
            return None

        for key, value in self.default_options.iteritems():
            options.setdefault(key, value)

        for key, attr in self.extra_options:
            value = getattr(settings, attr)
            if value != getattr(default_settings, attr):
                options.setdefault(key, value)

        # Generate a name for the thumbnail
        name = self._get_thumbnail_filename(source, geometry_string, options)

        # See if we've got a hit in the cache
        thumbnail = ImageFile(name, default.storage)
        cached = default.kvstore.get(thumbnail)
        if cached:
            return cached
        #fixing misssing 'image_info' to be compatible with sorl-thumbnail newewst version
        else:
            # We have to check exists() because the Storage backend does not
            # overwrite in some implementations.
            # so we make the assumption that if the thumbnail is not cached, it doesn't exist
            try:
                source_image = default.engine.get_image(source)
            except IOError:
                if settings.THUMBNAIL_DUMMY:
                    return DummyImageFile(geometry_string)
                else:
                    # if S3Storage says file doesn't exist remotely, don't try to
                    # create it and exit early.
                    # Will return working empty image type; 404'd image
                    logger.warn('Remote file [%s] at [%s] does not exist', file_, geometry_string)
                    return thumbnail

            # We might as well set the size since we have the image in memory
            image_info = default.engine.get_image_info(source_image)
            options['image_info'] = image_info

        # We cannot check if the file exists, as remote storage is slow. If
        # we have reached this point, the image does not exist in our kvstore
        # so create the entry and queue the generation of the image.
        #
        # Note: If the thumbnail file has been deleted, you will need to manually
        # clear the corresponding row from the kvstore to have thumbnail rebuilt.
        job = create_thumbnail.delay(file_, geometry_string, options, name)
        if job:
            geometry = parse_geometry(geometry_string)
            # We can't add a source row to the kvstore without the size
            # information being looked up, so add dummy information here
            # We'll need to correct this information when we generate the thumbnail
            source.set_size(geometry)
            default.kvstore.get_or_set(source)

            # We don't want to do any file access in this thread, so we tell sorlery
            # to proceed as normal and cheekily update the name and storage after
            # the hash has been calculated.
            thumbnail.set_size(geometry)
            default.kvstore.set(thumbnail, source)

            # Now we go back and manually update the thumbnail to point at the source image
            # Hopefully someone can suggest a better way to do this ... but the sorl internals
            # don't make it easy to.
            rawvalue = default.kvstore._get_raw(add_prefix(thumbnail.key))
            rawvaluearr = deserialize(rawvalue)
            rawvaluearr['name'] = file_.name
            default.kvstore._set_raw(add_prefix(thumbnail.key), serialize(rawvaluearr))

        thumbnail.name = file_.name
        return thumbnail
